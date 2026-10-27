"""The history: closed tickets stored, their timelines and journeys, the
similar-tickets and spam indexes (Qdrant and RAGent2 faked), the weekly run
and its report. Against the fake TicketAPI and a real database."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

import fake_t2 as fake
from support_agent import config, llm, options
from support_agent.history import index_sync, journey, similar, spam, weekly
from support_agent.history import store as history_store
from support_agent.ticketsystem import Note

FAR_PAST = datetime(2026, 1, 1, tzinfo=UTC)
LATER = datetime(2026, 12, 31, tzinfo=UTC)


@pytest.fixture
def values(db, t2, monkeypatch):
    monkeypatch.setattr(options, "_last_sync", float("-inf"))
    options.invalidate()
    options.sync(t2=t2, force=True)
    yield
    options.invalidate()


def _handled(t2, title="OTP not delivered", body="Our OTP messages are not delivered "
             "to Zain numbers since 9am. Please check.", sender="it@najd.sa") -> str:
    """An email ticket the way the team handles one: the agent routes it (with
    its own note), a person moves it on with a reason, notes, replies, and the
    customer confirms. Then it is closed."""
    tid = fake.add_email(title, body, sender)
    fake.T[tid]["contact"] = "Active"
    t2.update(tid, product="RiCH", queue="RiCH Support", type="Service Failure",
              note=Note("Support Agent: routed to RiCH Support", "AGENT REASONING ...",
                        sender="Support Agent"))          # as the writer sends it
    fake.staff(tid, "Checked the account, delivery reports show failures on Zain only.",
               internal=True, minutes=5)
    fake.reassign(tid, "RiCH Technical", reason="Needs the operator connection checked",
                  team="Technical", minutes=6)
    fake.staff(tid, "Dear customer,\n\nThe Zain route was restored. Please test again.\n\n"
               "Best regards\nOmar", internal=False, minutes=60)
    fake.customer(tid, "Works now, thank you.", minutes=90)
    fake.close(tid, minutes=95)
    return tid


def test_a_closed_ticket_becomes_history(db, t2, values):
    tid = _handled(t2)
    a = weekly.add_one(t2, tid, weekly.boilerplate_loader())
    c = db.query_one("SELECT * FROM closed_tickets WHERE id = %s", (a["id"],))
    assert (c["product_code"], c["queue"], c["contact_status"], c["noise_count"]) == (
        "RICH", "RiCH Technical", "Active", 2)   # our note, and their page's reassign note
    assert c["t2_closed_at"] is not None and c["solution_minutes"] > 0
    assert "OTP messages are not delivered" in c["search_text"]
    tl = db.query_one("SELECT * FROM ticket_timelines WHERE closed_ticket_id = %s", (c["id"],))
    assert tl["queue_path"] == ["RiCH Support", "RiCH Technical"]
    kinds = [e["kind"] for e in tl["entries"]]
    assert kinds == ["customer", "queue", "note", "queue", "reply", "customer"]
    move = tl["entries"][3]
    assert (move["queue"], move["reason"], move["team"]) == (
        "RiCH Technical", "Needs the operator connection checked", "Technical")
    assert "Support Agent" not in json.dumps(tl["entries"])   # the agent's note is not there
    assert "re-assigned" not in json.dumps(tl["entries"])
    assert db.query_one("SELECT count(*) AS n FROM closed_articles")["n"] == 4
    weekly.add_one(t2, tid, weekly.boilerplate_loader())       # again: nothing doubled
    assert db.query_one("SELECT count(*) AS n FROM closed_articles")["n"] == 4


def test_the_journey_keeps_to_the_recorded_path(db, t2, values, monkeypatch):
    a = weekly.add_one(t2, _handled(t2), weekly.boilerplate_loader())
    asked = []

    def model(messages, *, model=None, **_):
        asked.append(model)
        if model == config.get_settings().journey_model:
            raise llm.LLMError("model not served")
        return llm.Reply(json.dumps({
            "steps": [{"article": 5, "what": "Support replied the route was restored."},
                      {"article": 1, "what": "The customer reported OTP failures on Zain."}],
            "moves": [{"from": "RiCH Support", "to": "RiCH Technical",
                       "why": "The operator connection had to be checked."},
                      {"from": "RiCH Technical", "to": "Sales", "why": "invented"}],
            "summary": "OTP to one operator failing: the technical team restored the route."}))

    monkeypatch.setattr(llm, "complete", model)
    journey.for_ticket(a["id"])
    j = db.query_one("SELECT * FROM ticket_journeys")
    assert asked == [config.get_settings().journey_model,
                     config.get_settings().journey_fallback_model]
    assert j["model"] == config.get_settings().journey_fallback_model and j["fallback_reason"]
    assert [s["article"] for s in j["steps"]] == [1, 5]          # in message order
    assert j["moves"] == [{"from": "RiCH Support", "to": "RiCH Technical",
                           "why": "The operator connection had to be checked."}]
    text = journey.render(db.query_one("SELECT * FROM closed_tickets"),
                          db.query_one("SELECT * FROM ticket_timelines"))
    assert "Reason given: Needs the operator connection checked" in text
    assert "Omar" not in text and "omar@" not in text           # no names for the model


# ---------------------------------------------------------------------------
# the indexes, with Qdrant and RAGent2 faked
# ---------------------------------------------------------------------------

class FakeIndexes:
    """Qdrant's points per tenant, and searches answered from a table."""

    def __init__(self):
        self.points: dict[str, dict[str, dict]] = {}
        self.answers: dict[str, list[dict]] = {}

    def size(self, tenant):
        return len(self.points.get(tenant, {}))

    def search(self, tenant, text, limit):
        return [h for h in self.answers.get(tenant, []) if h["ticket_number"]
                in self.points.get(tenant, {})][:limit]

    def sync(self, tenant, doc_id, want, refresh=None):
        have = self.points.setdefault(tenant, {})
        removed = [n for n in have if n not in want]
        for n in removed:
            del have[n]
        added = [n for n in want if n not in have]
        for n in want:
            if n not in have or n in (refresh or set()):
                have[n] = want[n]
        return {"added": len(added), "refreshed": len(set(refresh or ()) & set(want)) -
                len(set(refresh or ()) & set(added)), "removed": len(removed),
                "total": len(want)}


@pytest.fixture
def indexes(monkeypatch):
    f = FakeIndexes()
    monkeypatch.setattr(index_sync, "size", f.size)
    monkeypatch.setattr(index_sync, "search", f.search)
    monkeypatch.setattr(index_sync, "sync", f.sync)
    monkeypatch.setenv("QDRANT_URL", "http://localhost:6333")
    config.env_settings.cache_clear()
    config.refresh_settings()
    return f


def _spam_ticket(t2, title, body, sender) -> str:
    tid = fake.add_email(title, body, sender)
    t2.mark_spam(tid)
    return tid


def test_the_weekly_run_and_its_report(db, t2, values, indexes, monkeypatch):
    monkeypatch.setattr(weekly, "journey_for", lambda cid: "a journey")
    real = _handled(t2)
    _spam_ticket(t2, "Grow your business", "Cheap SEO services, 50% off this week only.",
                 "offers@seo.biz")
    _spam_ticket(t2, "Grow your business!", "Cheap SEO services, 70% off this week only.",
                 "offers@seo.biz")                               # the same offer again
    r = weekly.run("2026-W43", send=False, start=FAR_PAST, end=LATER, t2=t2)
    assert (r["found"], r["added"], r["journeys"], r["failed"]) == (3, 3, 1, 0)
    assert r["indexes"]["spam_checked"] == {"checked": 2, "kept": 1, "repeat": 1}
    assert indexes.size(similar.tenant()) == 1 and indexes.size(spam.tenant()) == 1
    assert fake.T[real]["number"] in indexes.points[similar.tenant()]
    report = r["report"]
    assert "8. HISTORY UPDATED" in report and "Closed tickets added: 3 of 3 found" in report
    assert "History now holds 1 real and 2 spam tickets." in report
    run = db.query_one("SELECT * FROM weekly_runs")
    assert run["status"] == "done" and run["report"] == report
    assert weekly.run("2026-W43", send=False, t2=t2)["due"] is False     # once a week
    assert weekly.window_start(LATER + timedelta(days=7)) == LATER         # from where it stopped


def test_when_it_is_due():
    friday = datetime(2026, 10, 30, 4, 0, tzinfo=weekly.RIYADH)
    assert weekly.week_key(friday) == "2026-W44"
    assert weekly.due(friday - timedelta(hours=1)) is None                 # Friday 03:00
    assert weekly.due(friday - timedelta(days=1)) is None                  # Thursday


def test_first_run_reads_six_months_back(db):
    end = datetime(2026, 10, 30, tzinfo=UTC)
    assert weekly.window_start(end) == end - timedelta(days=180)


def test_similar_tickets_are_stored_with_the_draft(db, t2, values, indexes):
    a = weekly.add_one(t2, _handled(t2), weekly.boilerplate_loader())
    db.execute("INSERT INTO ticket_journeys (closed_ticket_id, path, summary, model) "
               "VALUES (%s, %s, 'Restored by the technical team.', 'm')",
               (a["id"], ["RiCH Support", "RiCH Technical"]))
    similar.sync()
    indexes.answers[similar.tenant()] = [{"ticket_number": a["ticket_number"], "text": "x",
                                          "score": 0.82},
                                         {"ticket_number": "T-NOT-STORED", "text": "y",
                                          "score": 0.5}]
    from support_agent.intake import store as intake_store
    new = intake_store.upsert(t2.get(fake.add_email("OTP failing", "OTP to Zain fails",
                                                    "ops@najd.sa")))
    d = db.query_one("INSERT INTO drafts (ticket_id, action, product, queue, summary) "
                     "VALUES (%s, 'route', 'RICH', 'RiCH Support', 's') RETURNING id",
                     (new.ticket_id,))["id"]
    assert similar.attach(d) == 1
    assert db.query_one("SELECT similar_status FROM drafts WHERE id = %s", (d,))[
        "similar_status"] == "found"
    row = similar.for_draft(d)[0]
    assert (row["ticket_number"], row["path"], row["journey"]) == (
        a["ticket_number"], ["RiCH Support", "RiCH Technical"], "Restored by the technical team.")


def test_no_index_yet_is_not_nothing_similar(db, t2, values):
    from support_agent.intake import store as intake_store
    new = intake_store.upsert(t2.get(fake.add_email("Help", "Something broke", "a@b.sa")))
    d = db.query_one("INSERT INTO drafts (ticket_id, action, product, queue, summary) "
                     "VALUES (%s, 'route', 'RICH', 'RiCH Support', 's') RETURNING id",
                     (new.ticket_id,))["id"]
    assert similar.attach(d) == 0
    assert db.query_one("SELECT similar_status FROM drafts")["similar_status"] == "no_index"


def test_the_spam_check(db, t2, values, indexes, monkeypatch):
    bp = weekly.boilerplate_loader()
    for i in range(5):
        weekly.add_one(t2, _spam_ticket(t2, f"Offer {i}", f"Buy {i} leads now cheap offer",
                                        "sales@leads.biz"), bp)
    weekly.add_one(t2, _spam_ticket(t2, "Win", "You won a prize claim now", "win@gmail.com"), bp)
    weekly.sync_indexes([])
    numbers = sorted(indexes.points[spam.tenant()])
    indexes.answers[spam.tenant()] = [{"ticket_number": numbers[0], "text": "Buy leads",
                                       "score": 0.95}]

    v = spam.check("Buy 1000 leads now", "sales@leads.biz")
    assert v.flagged and "almost the same as spam ticket" in v.reason
    assert "leads.biz has sent 5 spam tickets and no real ones" in v.reason
    assert "Looks like spam" in v.block()
    assert spam.sender_counts("gmail.com") == (0, 0)         # free mail says nothing
    indexes.answers[spam.tenant()][0]["score"] = 0.5
    indexes.answers[similar.tenant()] = []
    v = spam.check("Buy leads", "someone@new.sa")
    assert v.flagged and "closer to spam ticket" in v.reason  # 0.5 vs no real ticket
    indexes.answers[spam.tenant()][0]["score"] = 0.2
    v = spam.check("Our OTP fails", "it@najd.sa")
    assert not v.flagged and "Not enough to call it spam" in v.block()


def test_step_one_sees_the_spam_check(db, t2, values, indexes, monkeypatch):
    from support_agent.intake import store as intake_store
    from support_agent.orchestrator import pipeline
    from test_orchestrator import Script

    weekly.add_one(t2, _spam_ticket(t2, "Offer", "Cheap leads offer for you", "x@leads.biz"),
                   weekly.boilerplate_loader())
    weekly.sync_indexes([])
    number = next(iter(indexes.points[spam.tenant()]))
    indexes.answers[spam.tenant()] = [{"ticket_number": number, "text": "Cheap leads",
                                       "score": 0.97}]
    script = Script()
    script.product.update(spam=True, spam_reason="A leads offer like the ones marked spam")
    monkeypatch.setattr(llm, "complete", script)
    tid = intake_store.upsert(t2.get(fake.add_email("Leads", "Cheap leads offer again",
                                                    "y@leads.biz"))).ticket_id
    d = pipeline.draft(tid)
    assert "SPAM CHECK" in script.users["product"] and "Looks like spam" in script.users["product"]
    assert "Others at leads.biz:  spam 1" in script.users["product"]   # sender history
    row = db.query_one("SELECT * FROM draft_spam WHERE draft_id = %s", (d,))
    assert row["flagged"] and row["sender_spam"] == 1


def test_an_unblocked_sender_leaves_the_spam_index(db, t2, values, indexes):
    tid = _spam_ticket(t2, "Hello", "We would like to discuss a partnership offer", "p@co.sa")
    weekly.add_one(t2, tid, weekly.boilerplate_loader())
    weekly.sync_indexes([])
    assert indexes.size(spam.tenant()) == 1
    fake.T[tid]["contact"] = "Active"                 # it was a mistake: unblocked
    weekly.add_one(t2, tid, weekly.boilerplate_loader())
    weekly.sync_indexes([])
    assert indexes.size(spam.tenant()) == 0 and indexes.size(similar.tenant()) == 0
    assert db.query_one("SELECT spam_check FROM closed_tickets")["spam_check"] is None


def test_the_note_lists_similar_tickets(db, t2, values, indexes, monkeypatch):
    from support_agent.review import actions, writer
    a = weekly.add_one(t2, _handled(t2), weekly.boilerplate_loader())
    similar.sync()
    indexes.answers[similar.tenant()] = [{"ticket_number": a["ticket_number"], "text": "x",
                                          "score": 0.9}]
    from support_agent.intake import store as intake_store
    tid = fake.add_email("Zain OTP", "OTP to Zain numbers fails", "it@najd.sa")
    new = intake_store.upsert(t2.get(tid))
    d = db.query_one("INSERT INTO drafts (ticket_id, action, product, queue, summary) "
                     "VALUES (%s, 'route', 'RICH', 'RiCH Support', 's') RETURNING id",
                     (new.ticket_id,))["id"]
    similar.attach(d)
    monkeypatch.setenv("DRY_RUN", "false")
    config.env_settings.cache_clear()
    config.refresh_settings()
    actions.approve(d, who="sara@t2.sa")
    writer.send_pending(t2=t2)
    note = fake.T[tid]["threads"][-1]["body"]
    assert "SIMILAR CLOSED TICKETS" in note
    assert f"1. {a['ticket_number']}: OTP not delivered" in note
    assert "RiCH: RiCH Support -> RiCH Technical" in note     # the name, not the code


def test_history_products_include_the_intake(db):
    assert history_store.history_products()[-1] == "EMAIL"


def test_index_sync_against_qdrant(monkeypatch):
    """The comparison itself: what Qdrant holds against what the history says."""
    import types

    class Point:
        def __init__(self, pid, number):
            self.id, self.payload = pid, {"metadata": {"ticket_number": number}}

    class Client:
        def __init__(self):
            self.points = [Point(1, "T-1"), Point(2, "T-2"), Point(3, "T-2"), Point(4, "T-OLD")]
            self.deleted = []

        def scroll(self, **kw):
            return self.points, None

        def delete(self, collection_name, points_selector, wait):
            self.deleted += points_selector.points

    written = []
    writer = types.SimpleNamespace(ingest=lambda data, **kw: written.extend(
        d["metadata"]["ticket_number"] for d in data))
    client = Client()
    monkeypatch.setattr(index_sync, "_parts", lambda: (client, object(), writer, object()))
    want = {n: index_sync.entry("text", n, "doc") for n in ("T-1", "T-2", "T-3")}
    out = index_sync.sync("t2-precedent", "doc", want, refresh={"T-1"})
    assert sorted(client.deleted) == [3, 4]          # the double, and one no longer wanted
    assert sorted(written) == ["T-1", "T-3"]         # the missing one, and the refreshed
    assert out == {"added": 1, "refreshed": 1, "removed": 2, "total": 3}


def test_reading_search_results(db, monkeypatch):
    import types

    from support_agent.knowledge import retriever

    hits = [types.SimpleNamespace(text="Our OTP fails", score=0.81,
                                  metadata={"ticket_number": "T-9", "chunk_id": "T-9"}),
            types.SimpleNamespace(page_content="no number", score=0.5, metadata={})]
    asked = {}

    class Tenant:
        def search(self, text, top_k=None, use_reranker=True):
            asked.update(top_k=top_k, use_reranker=use_reranker)
            return hits

    monkeypatch.setattr(retriever, "_tenant", lambda name: Tenant())
    assert index_sync.search("t2-precedent", "otp", 3) == [
        {"ticket_number": "T-9", "text": "Our OTP fails", "score": 0.81}]
    assert asked["top_k"] == 3
