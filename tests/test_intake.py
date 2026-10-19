"""Intake against the fake TicketAPI and a real database."""

from __future__ import annotations

import fake_t2 as fake
from support_agent.intake import context, poller, store


def test_store_is_idempotent_and_keeps_new_articles(db, t2):
    tid = fake.add_email("OTP not delivered", "Hello,\n\nOur OTP to Zain fails.\n\nFaisal",
                         "IT@Najd.sa")
    first = store.upsert(t2.get(tid))
    assert first.is_new and first.articles_inserted == 1
    again = store.upsert(t2.get(tid))
    assert not again.is_new and again.articles_inserted == 0 and again.articles_skipped == 1
    row = db.query_one("SELECT * FROM tickets")
    assert row["t2_ticket_id"] == tid.lower() and row["product_code"] == "EMAIL"
    assert row["requires_classification"] and row["requester_email"] == "it@najd.sa"
    assert row["customer_contact_status"] == "Pending" and row["queue"] is None
    art = db.query_one("SELECT * FROM articles")
    assert art["party"] == "request" and art["via"] == "Email"
    assert art["sender_address"] == "it@najd.sa" and "\r" not in art["body_raw"]
    assert row["search_text"]                      # built for the similar-tickets search


def test_context_for_a_first_contact_email(db, t2):
    tid = fake.add_email("Renew sender", "نرغب في تجديد اسم المرسل ALWAHA.", "nora@alwaha.sa")
    stored = store.upsert(t2.get(tid))
    text = context.build(stored.ticket_id).render()
    assert "Product:     Unclassified Email" in text and "Queue:       (empty)" in text
    assert "not yet a known customer" in text
    assert "Pending Email Contacts" not in text   # their placeholder organisation, not real
    assert "تجديد اسم المرسل" in text


def test_poll_stores_without_deciding(db, t2):
    a = fake.add_email("One", "first", minutes_ago=20)
    b = fake.add_email("Two", "second", minutes_ago=10)
    r = poller.poll_once(decide=False, t2=t2)
    assert (r.seen, r.skipped, r.failed) == (2, 2, 0)
    assert {x["t2_ticket_id"] for x in db.query("SELECT t2_ticket_id FROM tickets")} == {
        a.lower(), b.lower()}


def test_poll_drafts_once_and_again_after_a_new_message(db, t2, monkeypatch):
    calls = []

    def fake_draft(ticket_id):
        newest = db.query_one("SELECT max(id) AS m FROM articles WHERE ticket_id = %s",
                              (ticket_id,))["m"]
        row = db.query_one(
            """INSERT INTO drafts (ticket_id, action, product, queue, next_state, summary,
                                   built_to_article_id)
               VALUES (%s, 'route', 'RiCH', 'RiCH Support', 'Open', 's', %s) RETURNING id""",
            (ticket_id, newest))
        calls.append(ticket_id)
        return row["id"]

    monkeypatch.setattr(poller, "_draft", fake_draft)
    tid = fake.add_email("One", "first")
    assert poller.poll_once(t2=t2).drafted == 1
    assert poller.poll_once(t2=t2).drafted == 0          # already covered
    db.execute("UPDATE drafts SET status = 'rejected'")
    assert poller.poll_once(t2=t2).drafted == 0          # a rejection is not a reason
    fake.T[tid]["threads"].append({"id": "11111111-1111-1111-1111-111111111111",
                                   "customer": True, "channel": "Email", "internal": False,
                                   "from": "client@example.com", "subject": "Re: One",
                                   "body": "any news?", "at": fake.now})
    assert poller.poll_once(t2=t2).drafted == 1          # the customer wrote again
    assert len(calls) == 2


def test_a_failed_draft_is_counted_not_fatal(db, t2, monkeypatch):
    def broken(ticket_id):
        raise RuntimeError("model down")

    monkeypatch.setattr(poller, "_draft", broken)
    fake.add_email("One", "first")
    r = poller.poll_once(t2=t2)
    assert (r.drafted, r.failed) == (0, 1)


def test_pending_draft_withdrawn_when_a_person_routed_the_ticket(db, t2, monkeypatch):
    monkeypatch.setattr(poller, "_draft", lambda ticket_id: db.query_one(
        """INSERT INTO drafts (ticket_id, action, product, queue, next_state, summary)
           VALUES (%s, 'route', 'RiCH', 'RiCH Support', 'Open', 's') RETURNING id""",
        (ticket_id,))["id"])
    taken = fake.add_email("Taken", "a person routes this one", minutes_ago=30)
    waiting = fake.add_email("Waiting", "still in the intake", minutes_ago=20)
    replied = fake.add_email("Replied", "staff replied, status changed", minutes_ago=10)
    poller.poll_once(t2=t2)
    assert db.query_one("SELECT count(*) AS n FROM drafts WHERE status = 'pending'")["n"] == 3

    t2.update(taken, product="RiCH", queue="RiCH Technical")          # on their screen
    t2.update(replied, state="Waiting For Customer Response")
    r = poller.poll_once(t2=t2)
    assert r.withdrawn == 1
    rows = {x["title"]: x for x in db.query(
        "SELECT t.title, d.status, t.queue FROM drafts d JOIN tickets t ON t.id = d.ticket_id")}
    assert rows["Taken"]["status"] == "superseded" and rows["Taken"]["queue"] == "RiCH Technical"
    assert rows["Waiting"]["status"] == "pending" and rows["Replied"]["status"] == "pending"
    assert waiting


def test_left_intake_reasons(t2):
    tid = fake.add_email("x", "y")
    assert poller.left_intake(t2.get(tid), "EMAIL") is None
    t2.update(tid, product="Ole5")
    assert poller.left_intake(t2.get(tid), "EMAIL") == "given the product Ole5"
