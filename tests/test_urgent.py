"""Urgent alerts: keywords and recipients per product, the check on a new
draft, and the email."""

from __future__ import annotations

import pytest

import fake_t2 as fake
from support_agent import config, options
from support_agent.intake import store as intake_store
from support_agent.notify import mail, urgent

ADMIN = "ai.admin@t2.sa"


@pytest.fixture
def values(db, t2, monkeypatch):
    monkeypatch.setattr(options, "_last_sync", float("-inf"))
    options.invalidate()
    options.sync(t2=t2, force=True)
    yield
    options.invalidate()


def _ticket(t2, body, title="Problem"):
    return intake_store.upsert(t2.get(fake.add_email(title, body, "it@najd.sa"))).ticket_id


def _draft(db, ticket_id, product="RICH", action="route", agent_reason=None):
    d = db.query_one("INSERT INTO drafts (ticket_id, action, product, queue, summary) "
                     "VALUES (%s, %s, %s, 'RiCH Technical', 's') RETURNING id",
                     (ticket_id, action, product))["id"]
    if agent_reason:
        db.execute("INSERT INTO draft_urgent (draft_id, by_agent, agent_reason) "
                   "VALUES (%s, true, %s)", (d, agent_reason))
    return d


def test_matching_like_a_person_reads():
    words = ["down", "متوقف", "outage"]
    assert urgent.matching_keywords("Please download the report", words) == []
    assert urgent.matching_keywords("The service is DOWN since 9", words) == ["down"]
    assert urgent.matching_keywords("النظام والمُتوقف منذ الصباح", words) == ["متوقف"]


def test_keywords_and_recipients_per_product(db, values):
    urgent.add_keyword("RICH", "OTP down", who=ADMIN)
    urgent.add_keyword(None, "outage", who=ADMIN)
    with pytest.raises(urgent.UrgentError, match="already"):
        urgent.add_keyword("rich", "  otp   DOWN ", who=ADMIN)
    with pytest.raises(urgent.UrgentError, match="not a product"):
        urgent.add_keyword("EMAIL", "x", who=ADMIN)
    assert [k["keyword"] for k in urgent.keywords("RICH")] == ["outage", "OTP down"]
    assert [k["keyword"] for k in urgent.keywords("OLE5")] == ["outage"]

    r = urgent.add_recipient("RICH", "oncall@t2.sa", "RiCH on call", who=ADMIN)
    with pytest.raises(urgent.UrgentError, match="not an email"):
        urgent.add_recipient("RICH", "oncall", who=ADMIN)
    urgent.remove_recipient(r["id"], who=ADMIN)
    assert urgent.recipients("RICH") == []
    rows = db.query("SELECT action, what, name FROM audit_log ORDER BY id")
    assert [(r["action"], r["what"]) for r in rows] == [
        ("add", "urgent keyword"), ("add", "urgent keyword"), ("add", "urgent recipient"),
        ("remove", "urgent recipient")]


def test_a_keyword_marks_the_draft_and_emails_the_products_people(db, t2, values):
    urgent.add_keyword("RICH", "OTP down", who=ADMIN)
    urgent.add_recipient("RICH", "rich-oncall@t2.sa", who=ADMIN)
    urgent.add_recipient(None, "manager@t2.sa", who=ADMIN)
    urgent.add_recipient("OLE5", "ole5-oncall@t2.sa", who=ADMIN)
    d = _draft(db, _ticket(t2, "Hello, OTP down for all our users since 9am."))
    r = urgent.check(d)
    assert r["keywords"] == ["OTP down"] and not r["by_agent"]
    assert sorted(e["to"] for e in r["emailed"]) == ["manager@t2.sa", "rich-oncall@t2.sa"]
    assert {e["status"] for e in r["emailed"]} == {"dry_run"}       # DRY_RUN is on
    row = db.query_one("SELECT * FROM draft_urgent WHERE draft_id = %s", (d,))
    assert row["by_keyword"] and row["keywords"] == ["OTP down"]
    mail_row = db.query_one("SELECT * FROM urgent_emails LIMIT 1")
    assert mail_row["subject"].startswith("[URGENT] RiCH: T-")
    assert "OTP down for all our users" in mail_row["body"]


def test_another_products_keyword_does_not_fire(db, t2, values):
    urgent.add_keyword("OLE5", "OTP down", who=ADMIN)
    assert urgent.check(_draft(db, _ticket(t2, "OTP down"))) is None


def test_the_agents_own_judgement_and_one_alert_a_day(db, t2, values):
    urgent.add_recipient(None, "manager@t2.sa", who=ADMIN)
    tid = _ticket(t2, "Our whole platform stopped.")
    r = urgent.check(_draft(db, tid, agent_reason="Platform down for every user"))
    assert r["by_agent"] and len(r["emailed"]) == 1
    assert "a model's call" in db.query_one("SELECT body FROM urgent_emails")["body"]
    db.execute("UPDATE drafts SET status = 'superseded'")
    again = urgent.check(_draft(db, tid, agent_reason="Still down"))
    assert again["skipped"] and again["emailed"] == []


def test_spam_is_never_urgent(db, t2, values):
    urgent.add_keyword(None, "urgent", who=ADMIN)
    assert urgent.check(_draft(db, _ticket(t2, "URGENT: claim your prize"),
                               product=None, action="spam")) is None


def test_the_email_goes_out_through_our_mail_server(db, t2, values, monkeypatch):
    sent = []

    class FakeSMTP:
        def __init__(self, host, port, timeout):
            sent.append(("connect", host, port))

        def ehlo(self):
            pass

        def starttls(self, context):
            sent.append(("starttls",))

        def login(self, user, password):
            sent.append(("login", user))

        def send_message(self, msg):
            sent.append(("send", msg["To"], msg["Subject"]))
            return {}

        def quit(self):
            pass

    monkeypatch.setattr(mail.smtplib, "SMTP", FakeSMTP)
    for k, v in (("DRY_RUN", "false"), ("SMTP_HOST", "localhost"), ("SMTP_PORT", "1025"),
                 ("SMTP_STARTTLS", "false"), ("MAIL_FROM", "agent@t2.sa")):
        monkeypatch.setenv(k, v)
    config.env_settings.cache_clear()
    config.refresh_settings()
    urgent.add_keyword(None, "outage", who=ADMIN)
    urgent.add_recipient(None, "manager@t2.sa", who=ADMIN)
    r = urgent.check(_draft(db, _ticket(t2, "Total outage")))
    assert r["emailed"] == [{"to": "manager@t2.sa", "status": "sent"}]
    assert ("starttls",) not in sent                    # a local catcher has none
    assert sent[-1][0:2] == ("send", "manager@t2.sa")
