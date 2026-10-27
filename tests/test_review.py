"""Reviewing drafts and writing them to the ticket, against the fake TicketAPI
and a real database."""

from __future__ import annotations

import json

import pytest

import fake_t2 as fake
from support_agent import config, llm, options
from support_agent.intake import store as intake_store
from support_agent.review import actions, signatures, writer
from test_orchestrator import Script

ADMIN = "ai.admin@t2.sa"
SARA = "Sara@T2.sa"


@pytest.fixture
def live(monkeypatch):
    """DRY_RUN off: the writer writes (to the fake)."""
    monkeypatch.setenv("DRY_RUN", "false")
    config.env_settings.cache_clear()
    config.refresh_settings()


@pytest.fixture
def ticket(db, t2, monkeypatch):
    monkeypatch.setattr(options, "_last_sync", float("-inf"))
    options.invalidate()
    fake.subtypes_with_reasons = True
    options.sync(t2=t2, force=True)
    tid = fake.add_email("تجديد اسم المرسل", "نرغب في تجديد اسم المرسل ALWAHA.",
                         "nora@alwaha-clinics.sa")
    stored = intake_store.upsert(t2.get(tid))
    yield {"t2": tid, "id": stored.ticket_id}
    options.invalidate()


def _draft(db, ticket, **values) -> int:
    row = {"action": "answer", "reply_kind": "answer", "reply_body": "مرحبا\n\nأرسلوا العقد.",
           "product": "RICH", "queue": "RiCH Support", "queue_reason": "sender names",
           "type": "Request", "subtype": "Sender Name - Renewal Contract",
           "conclusions": fake.REASONS["Sender Name - Renewal Contract"],
           "next_state": "Waiting For Customer Response", "priority": "Medium",
           "summary": "Renew the sender name ALWAHA", "reasoning": "A renewal request.",
           "missing": json.dumps(["the contract end date"]), "confidence": "high"} | values
    cols = ", ".join(row)
    marks = ", ".join(["%s"] * len(row))
    return db.query_one(f"INSERT INTO drafts (ticket_id, {cols}) VALUES (%s, {marks}) "
                        "RETURNING id", (ticket["id"], *row.values()))["id"]


def _active(ticket):
    """The sender, approved as a customer on their ticket page."""
    fake.T[ticket["t2"]]["contact"] = "Active"


def _writes(db, draft_id) -> dict:
    return {r["kind"]: r for r in db.query("SELECT * FROM ticket_writes WHERE draft_id = %s",
                                           (draft_id,))}


# ---------------------------------------------------------------------------
# approving
# ---------------------------------------------------------------------------

def test_approve_and_send(db, t2, ticket, live):
    _active(ticket)
    signatures.save("RICH", arabic="فريق دعم ريتش", english="RiCH Support Team", who=ADMIN)
    d = _draft(db, ticket)
    a = actions.approve(d, who=SARA, send=True, t2=t2)
    assert (a.outcome, a.writes) == ("approve", ["routing", "reply"])
    assert db.query_one("SELECT status FROM drafts WHERE id = %s", (d,))["status"] == "approved"

    assert writer.send_pending(t2=t2) == (2, 0)          # routing, then the reply
    t = fake.T[ticket["t2"]]
    assert (t["product"], t["queue"], t["subtype"], t["status"]) == (
        "RICH", "RiCH Support", "Sender Name - Renewal Contract",
        "Waiting For Customer Response")
    note, reply = t["threads"][-2:]
    assert note["internal"] and "Approved by sara@t2.sa" in note["body"]
    body = note["body"]
    order = [body.index(x) for x in ("REQUEST", "CLASSIFICATION", "QUEUE REASON",
                                     "AGENT REASONING", "STILL TO FIND OUT", "SUGGESTED REPLY")]
    assert order == sorted(order) and "Sent to the customer as its own message." in body
    assert body.count("=" * 60) == 6                 # a rule before each section
    assert note["from"] == "Support Agent"          # not shown as the customer
    assert not reply["internal"] and reply["body"].endswith("فريق دعم ريتش")   # Arabic reply
    mail = fake.sent_mail[-1]
    assert mail["to"] == "nora@alwaha-clinics.sa" and mail["subject"].endswith(
        "Re: تجديد اسم المرسل")
    w = _writes(db, d)
    assert w["routing"]["status"] == w["reply"]["status"] == "sent"
    assert w["reply"]["message_id"] == mail["message_id"]
    assert writer.send_pending(t2=t2) == (0, 0)          # nothing twice
    row = db.query_one("SELECT * FROM audit_log WHERE action = 'approve'")
    assert (row["who"], row["name"]) == ("sara@t2.sa", t["number"])


def test_approve_and_draft_keeps_the_reply_in_the_note(db, t2, ticket, live):
    signatures.save(None, arabic=None, english="T2 Support", who=ADMIN)   # every product
    d = _draft(db, ticket, reply_body="Hello,\n\nPlease send the signed contract.")
    a = actions.approve(d, who=SARA, send=False)
    assert a.writes == ["routing"]
    writer.send_pending(t2=t2)
    note = fake.T[ticket["t2"]]["threads"][-1]
    assert note["internal"] and "The reply below was NOT sent." in note["body"]
    assert "Not sent: check it and send it from this page." in note["body"]
    assert "Please send the signed contract.\n\nT2 Support" in note["body"]
    assert fake.sent_mail == []


def test_approve_and_send_waits_for_a_pending_contact(db, t2, ticket):
    d = _draft(db, ticket)
    with pytest.raises(actions.ReviewError, match="pending contact"):
        actions.approve(d, who=SARA, send=True, t2=t2)
    _active(ticket)                                  # approved on their page since
    assert actions.approve(d, who=SARA, send=True, t2=t2).writes == ["routing", "reply"]
    assert db.query_one("SELECT customer_contact_status FROM tickets")[
        "customer_contact_status"] == "Active"       # our copy caught up


def test_a_routing_keeps_the_agents_reply_in_the_note(db, t2, ticket, live):
    d = _draft(db, ticket, action="route", reply_kind=None, reply_body=None,
               evidence=json.dumps({"held_reply": {"body": "Please send the contract."}}))
    actions.approve(d, who=SARA)
    writer.send_pending(t2=t2)
    note = fake.T[ticket["t2"]]["threads"][-1]["body"]
    assert "Routed; nothing was sent to the customer." in note
    assert "Not sent: the ticket was routed." in note and "Please send the contract." in note


def test_the_note_says_why_urgent_and_who_was_alerted(db, t2, ticket, live):
    first = _draft(db, ticket, status="superseded")
    d = _draft(db, ticket)
    db.execute("INSERT INTO draft_urgent (draft_id, by_keyword, keywords, by_agent, agent_reason) "
               "VALUES (%s, true, %s, true, 'Sender names stopped')", (d, ["متوقف"]))
    for to, status in [("oncall@t2.sa", "sent"), ("manager@t2.sa", "failed")]:
        db.execute("INSERT INTO urgent_emails (draft_id, ticket_id, to_address, subject, body, "
                   "status) VALUES (%s, %s, %s, 's', 'b', %s)", (first, ticket["id"], to, status))
    actions.approve(d, who=SARA)
    writer.send_pending(t2=t2)
    note = fake.T[ticket["t2"]]["threads"][-1]["body"]
    assert ("URGENT\nKeywords: متوقف\nThe agent: Sender names stopped\n"
            "Alert sent to: oncall@t2.sa") in note       # an earlier draft's alert counts


def test_a_route_has_no_reply_to_send(db, ticket):
    d = _draft(db, ticket, action="route", reply_kind=None, reply_body=None)
    with pytest.raises(actions.ReviewError, match="no reply to send"):
        actions.approve(d, who=SARA, send=True)
    assert actions.approve(d, who=SARA).writes == ["routing"]


def test_approved_once(db, ticket):
    d = _draft(db, ticket)
    actions.approve(d, who=SARA)
    with pytest.raises(actions.NotPending):
        actions.approve(d, who="other@t2.sa")
    with pytest.raises(actions.NotPending):
        actions.reject(d, who="other@t2.sa", reason="late")


# ---------------------------------------------------------------------------
# editing
# ---------------------------------------------------------------------------

def test_edit_is_checked_and_recorded(db, t2, ticket, live):
    d = _draft(db, ticket)
    with pytest.raises(actions.ReviewError, match="not a queue of RICH"):
        actions.approve(d, who=SARA, changes={"queue": "Ole5 Support"})
    with pytest.raises(actions.ReviewError, match="cannot change"):
        actions.approve(d, who=SARA, changes={"summary": "x"})

    a = actions.approve(d, who=SARA, changes={"queue": "rich billing", "type": "Inquiry",
                                              "subtype": "DLR - Call Barring"},
                        comment="Billing handles these now.")
    assert a.outcome == "edit"
    review = db.query_one("SELECT * FROM reviews WHERE draft_id = %s", (d,))
    assert review["final"]["queue"] == "RiCH Billing"                    # their spelling
    assert review["final"]["conclusions"] == fake.REASONS["DLR - Call Barring"]
    assert review["changes"]["queue"] == {"from": "RiCH Support", "to": "RiCH Billing"}
    assert set(review["changed_fields"]) >= {"queue", "type", "subtype", "conclusions"}
    writer.send_pending(t2=t2)
    note = fake.T[ticket["t2"]]["threads"][-1]["body"]
    assert "Queue: RiCH Support -> RiCH Billing" in note and "Billing handles these" in note
    row = db.query_one("SELECT * FROM audit_log WHERE action = 'edit'")
    assert "queue: RiCH Support -> RiCH Billing" in row["old_value"]


def test_changing_the_type_clears_a_subtype_that_no_longer_fits(db, ticket):
    d = _draft(db, ticket)
    actions.approve(d, who=SARA, changes={"type": "Service Failure"})
    final = db.query_one("SELECT final FROM reviews")["final"]
    assert (final["type"], final["subtype"], final["conclusions"]) == (
        "Service Failure", None, None)                # the old subtype's reason goes too


def test_changing_the_product_needs_its_own_queue(db, ticket):
    d = _draft(db, ticket)
    with pytest.raises(actions.ReviewError, match="not a queue of OLE5"):
        actions.approve(d, who=SARA, changes={"product": "Ole5"})
    a = actions.approve(d, who=SARA, changes={"product": "Ole5", "queue": "Ole5 Technical",
                                              "type": "Inquiry"})
    final = db.query_one("SELECT final FROM reviews")["final"]
    assert (final["product"], final["queue"], final["subtype"]) == (
        "OLE5", "Ole5 Technical", None)
    assert a.outcome == "edit"


def test_a_route_made_an_answer_with_the_kept_reply(db, ticket):
    d = _draft(db, ticket, action="route", reply_kind=None, reply_body=None)
    with pytest.raises(actions.ReviewError, match="needs a reply"):
        actions.approve(d, who=SARA, changes={"action": "answer"})
    a = actions.approve(d, who=SARA, changes={"action": "answer",
                                              "reply_body": "Please send the contract."})
    assert a.writes == ["routing"] and "reply_body" in a.changed_fields


# ---------------------------------------------------------------------------
# rejecting, drafting again, spam
# ---------------------------------------------------------------------------

def test_reject_writes_nothing(db, t2, ticket, live):
    d = _draft(db, ticket)
    with pytest.raises(actions.ReviewError, match="say why"):
        actions.reject(d, who=SARA, reason=" ")
    actions.reject(d, who=SARA, reason="Not a renewal")
    assert writer.send_pending(t2=t2) == (0, 0)
    assert db.query_one("SELECT outcome, justification FROM reviews")["justification"] == (
        "Not a renewal")


def test_draft_again_for_another_product(db, ticket, monkeypatch):
    script = Script()
    script.decision.update(queue="Ole5 Support", type="Inquiry")
    script.subtype.update(subtype="Login")
    monkeypatch.setattr(llm, "complete", script)
    d = _draft(db, ticket)
    new = actions.redraft(d, who=SARA, product="Ole5")
    old_row = db.query_one("SELECT status FROM drafts WHERE id = %s", (d,))
    new_row = db.query_one("SELECT * FROM drafts WHERE id = %s", (new,))
    assert old_row["status"] == "rejected" and new_row["status"] == "pending"
    assert (new_row["product"], new_row["replaces_draft_id"]) == ("OLE5", d)
    review = db.query_one("SELECT * FROM reviews WHERE draft_id = %s", (d,))
    assert review["justification"].startswith("wrong product: RICH -> OLE5")


def test_draft_again_that_fails_leaves_the_old_draft_waiting(db, ticket, monkeypatch):
    def broken(*a, **k):
        raise llm.LLMError("model down")
    monkeypatch.setattr(llm, "complete", broken)
    d = _draft(db, ticket)
    with pytest.raises(Exception):
        actions.redraft(d, who=SARA, product="OLE5")
    assert db.query_one("SELECT status FROM drafts WHERE id = %s", (d,))["status"] == "pending"


def _spam(db, ticket) -> int:
    return _draft(db, ticket, action="spam", product=None, reply_kind=None, reply_body=None,
                  queue=None, type=None, subtype=None, conclusions=None, next_state=None,
                  priority=None)


def test_spam_blocks_the_sender_and_closes_their_tickets(db, t2, ticket, live):
    other = fake.add_email("Another offer", "Cheap SEO", "nora@alwaha-clinics.sa")
    d = _spam(db, ticket)
    with pytest.raises(actions.ReviewError):
        actions.approve(d, who=SARA, send=True)
    a = actions.approve(d, who=SARA, t2=t2)
    assert a.writes == ["spam"]
    assert writer.send_pending(t2=t2) == (1, 0)
    w = _writes(db, d)["spam"]
    assert w["status"] == "sent" and w["payload"]["tickets_closed"] == 2
    for tid in (ticket["t2"], other):
        assert (fake.T[tid]["contact"], fake.T[tid]["status"]) == ("Blocked", "closed successful")
    assert db.query_one("SELECT customer_contact_status FROM tickets")[
        "customer_contact_status"] == "Blocked"
    assert fake.T[ticket["t2"]]["threads"][-1]["customer"]      # no note, no email
    assert writer.send_pending(t2=t2) == (0, 0)                 # never twice


def test_spam_is_refused_for_a_sender_approved_since(db, t2, ticket, live):
    d = _spam(db, ticket)
    _active(ticket)                                           # approved on their page
    with pytest.raises(actions.ReviewError, match="no longer a pending contact"):
        actions.approve(d, who=SARA, t2=t2)
    db.execute("UPDATE drafts SET status = 'superseded' WHERE id = %s", (d,))
    db.execute("UPDATE tickets SET customer_contact_status = 'Pending'")
    fake.T[ticket["t2"]]["contact"] = "Pending"
    d2 = _spam(db, ticket)
    actions.approve(d2, who=SARA, t2=t2)
    _active(ticket)                                           # approved after our approval
    writer.send_pending(t2=t2)
    w = _writes(db, d2)["spam"]
    assert w["status"] == "failed" and "no longer a pending contact" in w["last_error"]
    assert fake.T[ticket["t2"]]["status"] == "Open"


def test_spam_with_no_answer_is_checked_on_the_ticket(db, t2, ticket, live):
    d = _spam(db, ticket)
    actions.approve(d, who=SARA, t2=t2)
    fake.fail_next.append(502)                  # no answer: the sender is still pending
    assert writer.send_pending(t2=t2) == (0, 1)
    assert _writes(db, d)["spam"]["status"] == "pending"     # so it did not happen: again
    assert writer.send_pending(t2=t2) == (1, 0)
    assert fake.T[ticket["t2"]]["contact"] == "Blocked"


def test_spam_the_reviewer_disagrees_with_is_routed(db, ticket):
    d = _spam(db, ticket)
    with pytest.raises(actions.ReviewError, match="choose the product"):
        actions.approve(d, who=SARA, changes={"action": "route"})
    a = actions.approve(d, who=SARA, changes={"action": "route", "product": "RICH",
                                              "queue": "RiCH Support"})
    assert a.writes == ["routing"] and a.outcome == "edit"


# ---------------------------------------------------------------------------
# the writer
# ---------------------------------------------------------------------------

def test_dry_run_writes_nothing(db, t2, ticket):
    actions.approve(_draft(db, ticket), who=SARA)
    assert writer.send_pending(t2=t2) == (0, 0)
    assert db.query_one("SELECT status FROM ticket_writes")["status"] == "pending"
    assert fake.T[ticket["t2"]]["queue"] is None


@pytest.mark.parametrize("change, why", [
    ({"queue": "RiCH Technical"}, "put in RiCH Technical"),      # a person routed it first
    ({"assigned_staff": "omar@t2.sa"}, "assigned to omar@t2.sa"),   # or took it
])
def test_a_ticket_someone_handled_meanwhile_is_left_alone(db, t2, ticket, live, change, why):
    _active(ticket)
    d = _draft(db, ticket)
    actions.approve(d, who=SARA, send=True, t2=t2)
    fake.T[ticket["t2"]].update(change)
    writer.send_pending(t2=t2)
    w = _writes(db, d)
    assert w["routing"]["status"] == "failed" and why in w["routing"]["last_error"]
    assert w["reply"]["status"] == "failed" and fake.sent_mail == []
    assert fake.T[ticket["t2"]]["product"] == "EMAIL"


def test_a_reply_held_until_the_contact_is_approved(db, t2, ticket, live):
    d = _draft(db, ticket)
    db.execute("UPDATE tickets SET customer_contact_status = 'Active'")   # our copy: wrong
    actions.approve(d, who=SARA, send=True, t2=t2)
    assert writer.send_pending(t2=t2) == (1, 1)              # routing written, reply held
    w = _writes(db, d)["reply"]
    assert w["status"] == "held" and "approve or link the contact" in w["last_error"]
    with pytest.raises(actions.ReviewError):
        actions.settle(w["id"], who=SARA, happened=True)      # not uncertain
    _active(ticket)
    actions.send_again(w["id"], who=SARA)
    assert writer.send_pending(t2=t2) == (1, 0)
    assert _writes(db, d)["reply"]["status"] == "sent" and len(fake.sent_mail) == 1


def test_a_reply_refused_before_sending_is_held(db, t2, ticket, live):
    _active(ticket)
    d = _draft(db, ticket)
    actions.approve(d, who=SARA, send=True, t2=t2)
    fake.mail_fails.append("not_attempted")              # say, no customer address
    assert writer.send_pending(t2=t2) == (1, 1)
    w = _writes(db, d)["reply"]
    assert w["status"] == "held" and "check the customer's email address" in w["last_error"]
    actions.send_again(w["id"], who=SARA)
    assert writer.send_pending(t2=t2) == (1, 0) and len(fake.sent_mail) == 1


def test_an_email_that_may_have_gone_is_never_sent_twice(db, t2, ticket, live):
    _active(ticket)
    d = _draft(db, ticket)
    actions.approve(d, who=SARA, send=True, t2=t2)
    fake.mail_fails.append("send")
    writer.send_pending(t2=t2)
    w = _writes(db, d)["reply"]
    assert w["status"] == "uncertain" and "Sent folder" in w["last_error"]
    assert writer.send_pending(t2=t2) == (0, 0)              # waits for a person
    actions.settle(w["id"], who=SARA, happened=False)         # checked: it did not go
    assert writer.send_pending(t2=t2) == (1, 0)
    assert len(fake.sent_mail) == 1


def test_their_system_down_is_tried_again(db, t2, ticket, live):
    d = _draft(db, ticket)
    actions.approve(d, who=SARA)
    fake.fail_next.extend([503, 503, 503])                    # the ticket read fails
    assert writer.send_pending(t2=t2) == (0, 1)
    w = _writes(db, d)["routing"]
    assert (w["status"], w["attempts"]) == ("pending", 1)
    assert writer.send_pending(t2=t2) == (1, 0)


# ---------------------------------------------------------------------------
# signatures
# ---------------------------------------------------------------------------

def test_signature_by_product_and_language(db):
    assert signatures.sign("RICH", "Hello") == "Hello"           # none set
    signatures.save(None, arabic="فريق الدعم", english="Support Team", who=ADMIN)
    signatures.save("RICH", arabic=None, english="RiCH Support", who=ADMIN)
    assert signatures.sign("RICH", "مرحبا بك") == "مرحبا بك\n\nRiCH Support"   # only English
    assert signatures.sign("OLE5", "مرحبا بك") == "مرحبا بك\n\nفريق الدعم"
    assert signatures.sign("OLE5", "Hello") == "Hello\n\nSupport Team"
    signatures.save("RICH", arabic=None, english=None, who=ADMIN)    # removed
    assert signatures.get("RICH") is None
    rows = db.query("SELECT name, new_value FROM audit_log WHERE what = 'signature' "
                    "ORDER BY id")
    assert [r["name"] for r in rows] == ["all products", "RICH", "RICH"]
    assert rows[-1]["new_value"] is None
