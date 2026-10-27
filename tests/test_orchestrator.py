"""The three steps, with the model and the knowledge base scripted, against a
real database and the fake TicketAPI."""

from __future__ import annotations

import json

import pytest

import fake_t2 as fake
from support_agent import llm, options
from support_agent.intake import store as intake_store
from support_agent.knowledge import retriever
from support_agent.knowledge.contracts import Evidence, Retrieval, Verdict
from support_agent.orchestrator import pipeline, validate


class Script:
    """Stands in for llm.complete. Knows the step from the system prompt."""

    def __init__(self):
        self.product = {"product": "RICH", "sure": True, "reason": "Sender names are RiCH",
                        "summary": "Renew the sender name ALWAHA."}
        self.searches = ["How is a sender name contract renewed?"]
        self.decision = {
            "action": "answer", "reply_kind": "answer", "reply_body": "مرحبا\n\nأرسلوا العقد.",
            "queue": "RiCH Support", "queue_reason": "Sender name requests", "type": "Request",
            "next_state": "Waiting For Customer Response", "priority": "Medium",
            "issue_type": "sender name renewal", "summary": "Renew sender name ALWAHA",
            "collected": {"sender name": "ALWAHA"}, "missing": [], "confidence": "high",
            "reasoning": "The guide says which documents are needed."}
        self.subtype = {"subtype": "Sender Name - Renewal Contract", "why": "A renewal",
                        "conclusions": "Request - renew the sender name ALWAHA"}
        self.calls: list[tuple[str, bool, bool]] = []
        self.users: dict[str, str] = {}
        self._left: list[str] = []

    @staticmethod
    def step(messages) -> str:
        first = messages[0]["content"]
        if first.startswith("You read one email"):
            return "product"
        if first.startswith("You choose the subtype"):
            return "subtype"
        return "route"

    def __call__(self, messages, *, tools=None, json_mode=False, temperature=0.0):
        step = self.step(messages)
        self.calls.append((step, json_mode, bool(tools)))
        self.users.setdefault(step, messages[1]["content"])
        if step == "route" and not json_mode:
            if not any(m["role"] == "tool" for m in messages) and not self._left:
                self._left = list(self.searches)
            if self._left:
                q = self._left.pop(0)
                return llm.Reply("", [llm.ToolCall(f"c{len(self.calls)}",
                                                   "search_knowledge_base", {"question": q})])
            return llm.Reply("Enough to decide.")
        body = {"product": self.product, "route": self.decision, "subtype": self.subtype}[step]
        if callable(body):
            body = body()
        return llm.Reply(json.dumps(body, ensure_ascii=False), usage={"prompt": 10,
                                                                      "completion": 5})


def _found(relation="DIRECT"):
    def retrieve(question, code):
        return Retrieval(query=question, verdict=Verdict.ANSWERABLE if relation == "DIRECT"
                         else Verdict.PARTIAL,
                         evidence=[Evidence(text="Send the signed renewal contract.",
                                            document=f"{code}-sender-names.md", section=None,
                                            source_file=None, relation=relation, score=3,
                                            reason="answers it")])
    return retrieve


@pytest.fixture
def script(monkeypatch):
    s = Script()
    monkeypatch.setattr(llm, "complete", s)
    return s


@pytest.fixture
def kb(monkeypatch):
    """A knowledge base with documents, answering directly."""
    asked = []
    monkeypatch.setattr(retriever, "unavailable", lambda code: None)

    def retrieve(question, code):
        asked.append((question, code))
        return _found()(question, code)

    monkeypatch.setattr(retriever, "retrieve", retrieve)
    return asked


@pytest.fixture
def ticket(db, t2, monkeypatch):
    monkeypatch.setattr(options, "_last_sync", float("-inf"))
    options.invalidate()
    options.sync(t2=t2, force=True)
    tid = fake.add_email("تجديد اسم المرسل", "نرغب في تجديد اسم المرسل ALWAHA.",
                         "marketing@alwaha-clinics.sa")
    yield intake_store.upsert(t2.get(tid)).ticket_id
    options.invalidate()


def _draft(db, draft_id):
    return db.query_one("SELECT * FROM drafts WHERE id = %s", (draft_id,))


def test_three_steps_make_one_draft(db, ticket, script, kb):
    d = _draft(db, pipeline.draft(ticket))
    assert (d["action"], d["reply_kind"], d["product"], d["product_sure"]) == (
        "answer", "answer", "RICH", True)
    assert (d["queue"], d["type"], d["subtype"]) == (
        "RiCH Support", "Request", "Sender Name - Renewal Contract")
    # The fake catalog has no reasons yet: the model writes Conclusions.
    assert d["conclusions"] == "Request - renew the sender name ALWAHA"
    assert "write `conclusions` yourself" in script.users["subtype"]
    # Step 3 is told what step 2 concluded, in our words.
    for line in ("Product:   RiCH (RICH)", "Queue:     RiCH Support  (Sender name requests)",
                 "Issue:     sender name renewal",
                 "Reasoning: The guide says which documents are needed."):
        assert line in script.users["subtype"]
    assert d["status"] == "pending" and d["built_to_article_id"]
    assert kb == [("How is a sender name contract renewed?", "RICH")]   # RiCH's own KB
    assert [c[0] for c in script.calls] == ["product", "route", "route", "route", "subtype"]
    steps = [t["step"] for t in d["trace"]]
    assert steps[0] == "started" and steps[-1] == "done" and "search" in steps
    assert d["trace"][-1]["tokens"]["prompt"] > 0
    assert d["evidence"]["verdict"] == "answerable" and len(d["evidence"]["searches"]) == 1
    # Step 2 is told the product, and offered only that product's queues.
    assert "Chosen: RiCH (RICH)" in script.users["route"]


def test_conclusions_is_the_subtype_reason_word_for_word(db, t2, ticket, script, kb):
    fake.subtypes_with_reasons = True
    options.sync(t2=t2, force=True)
    d = _draft(db, pipeline.draft(ticket))
    assert d["conclusions"] == fake.REASONS["Sender Name - Renewal Contract"]


def test_spam_stops_after_the_product_step(db, ticket, script, kb):
    script.product.update(spam=True, spam_reason="An SEO offer")
    d = _draft(db, pipeline.draft(ticket))
    assert (d["action"], d["product"], d["queue"], d["reasoning"]) == (
        "spam", None, None, "An SEO offer")             # the model's product guess: dropped
    assert [c[0] for c in script.calls] == ["product"] and kb == []


def test_spam_is_not_proposed_for_a_known_customer(db, ticket, script, kb):
    db.execute("UPDATE tickets SET customer_contact_status = 'Active'")
    script.product.update(spam=True, spam_reason="Reads like marketing")
    d = _draft(db, pipeline.draft(ticket))
    assert d["action"] == "answer"
    assert any("known customer" in c for c in d["evidence"]["corrections"])


def test_unsure_product_is_kept_for_the_reviewer(db, ticket, script, kb):
    script.product.update(sure=True, other="Ole5")
    assert _draft(db, pipeline.draft(ticket))["product_sure"] is False


def test_a_product_not_listed_is_asked_again_then_fails(db, ticket, script, kb):
    answers = iter([{**script.product, "product": "Souq"}, script.product])
    script.product = lambda: next(answers)
    assert _draft(db, pipeline.draft(ticket))["product"] == "RICH"

    script.product = {**Script().product, "product": "Souq"}
    with pytest.raises(pipeline.DecisionFailed):
        pipeline.draft(ticket)
    assert db.query_one("SELECT count(*) AS n FROM drafts WHERE status = 'pending'")["n"] == 1


def test_without_documents_it_cannot_answer(db, ticket, script):
    # No QDRANT_URL in tests: the knowledge base is unavailable.
    script.decision["next_state"] = "Open"
    d = _draft(db, pipeline.draft(ticket))
    assert all(not tools for step, _, tools in script.calls if step == "route")
    assert (d["action"], d["reply_body"]) == ("route", None)
    assert d["next_state"] == "Waiting for Concerned Department"
    assert "answer without searching the knowledge base: routed" in d["evidence"]["corrections"]
    held = d["evidence"]["held_reply"]                   # kept, in case it becomes an answer
    assert held["body"].startswith("مرحبا")
    assert held["why"] == "answer without searching the knowledge base"


def test_a_route_keeps_the_reply_it_wrote(db, ticket, script, kb):
    script.decision.update(action="route", reply_kind="ask_more")
    d = _draft(db, pipeline.draft(ticket))
    assert (d["action"], d["reply_body"], d["reply_kind"]) == ("route", None, None)
    assert d["evidence"]["held_reply"] == {
        "kind": "ask_more", "body": "مرحبا\n\nأرسلوا العقد.",
        "next_state": "Waiting For Customer Response", "documents": ["RICH-sender-names.md"],
        "why": "the agent chose to route"}


@pytest.mark.parametrize("state", ["New", "open"])
def test_a_routing_is_never_left_new_or_open(db, ticket, script, kb, state):
    script.decision.update(action="route", reply_kind=None, reply_body=None, next_state=state)
    d = _draft(db, pipeline.draft(ticket))
    assert d["next_state"] == "Waiting for Concerned Department"
    assert any(c.startswith("next_state:") for c in d["evidence"]["corrections"])


def test_inferential_evidence_holds_the_reply_back(db, ticket, script, kb, monkeypatch):
    monkeypatch.setattr(retriever, "retrieve", _found("INFERENTIAL"))
    d = _draft(db, pipeline.draft(ticket))
    assert d["action"] == "route" and d["reply_body"] is None
    assert d["evidence"]["held_reply"]["body"].startswith("مرحبا")


def test_drafting_again_for_another_product(db, ticket, script, kb):
    first = pipeline.draft(ticket)
    db.execute("UPDATE drafts SET status = 'rejected' WHERE id = %s", (first,))
    script.calls.clear()
    script.decision.update(queue="Ole5 Support", type="Inquiry")
    script.subtype.update(subtype="Login")
    d = _draft(db, pipeline.draft(ticket, product="OLE5", replaces=first))
    assert (d["product"], d["replaces_draft_id"], d["subtype"]) == ("OLE5", first, "Login")
    assert [c[0] for c in script.calls][0] == "route"     # no product step


def test_one_subtype_with_its_reason_needs_no_call(db, t2, ticket, script, kb):
    fake.subtypes_with_reasons = True
    fake.REASONS["Login"] = "A user cannot sign in."
    try:
        options.sync(t2=t2, force=True)
        script.product.update(product="OLE5")
        script.decision.update(queue="Ole5 Support", type="Inquiry")
        d = _draft(db, pipeline.draft(ticket))
        assert (d["subtype"], d["conclusions"]) == ("Login", "A user cannot sign in.")
        assert "subtype" not in [c[0] for c in script.calls]
    finally:
        del fake.REASONS["Login"]


def test_a_product_without_queues_stops_after_the_product_step(db, t2, script, kb,
                                                                monkeypatch):
    fake.PRODUCTS["SOUQT2"] = {"name": "Souq T2", "queues": [], "types": {}}
    try:
        monkeypatch.setattr(options, "_last_sync", float("-inf"))
        options.invalidate()
        options.sync(t2=t2, force=True)
        tid = intake_store.upsert(t2.get(fake.add_email("Order", "Where is my order?"))).ticket_id
        script.product.update(product="SOUQT2")
        d = _draft(db, pipeline.draft(tid))
        assert (d["action"], d["product"], d["queue"]) == ("route", "SOUQT2", None)
        assert "no queues" in d["note"]
    finally:
        del fake.PRODUCTS["SOUQT2"]
        options.invalidate()


def test_a_wrong_queue_is_corrected_and_marked_low(db, ticket, script, kb):
    script.decision.update(queue="Billing", priority="2. Medium")
    d = _draft(db, pipeline.draft(ticket))
    assert (d["queue"], d["confidence"], d["priority"]) == ("RiCH Support", "low", "Medium")


def test_wrong_values_use_the_defaults_chosen_at_the_first_sync(db, ticket, script, kb):
    script.decision.update(queue="Billing", type="Complaint", priority="Urgent!!")
    script.subtype.update(subtype="DLR - Call Barring")
    d = _draft(db, pipeline.draft(ticket))
    assert (d["queue"], d["type"], d["subtype"], d["priority"], d["confidence"]) == (
        "RiCH Support", "Inquiry", "DLR - Call Barring", "Low", "low")


def test_values_that_are_not_real_fall_back_to_the_admins_defaults(db, ticket, script, kb):
    for kind, value, product in (("queue", "RiCH Technical", "RICH"),
                                 ("type", "Service Failure", "RICH"),
                                 ("priority", "High", None)):
        options.set_default(options.find(kind, value, product=product).id,
                            who="ai.admin@t2.sa")
    script.decision.update(queue="Billing", type="Complaint", next_state="Escalated",
                           priority="Urgent!!")
    script.subtype.update(subtype="Operator - Outage - OTP")
    d = _draft(db, pipeline.draft(ticket))
    assert (d["queue"], d["type"], d["subtype"], d["priority"]) == (
        "RiCH Technical", "Service Failure", "Operator - Outage - OTP", "High")
    assert d["next_state"] == "Open"          # the default status; an answer keeps it
    assert "type: 'Complaint' is not a type, using the default 'Service Failure'" in (
        d["evidence"]["corrections"])


def test_urgent_is_kept_with_its_reason(db, ticket, script, kb):
    script.decision.update(urgent=True, urgent_reason="OTP down for all their users")
    draft_id = pipeline.draft(ticket)
    row = db.query_one("SELECT * FROM draft_urgent WHERE draft_id = %s", (draft_id,))
    assert row["by_agent"] and row["agent_reason"] == "OTP down for all their users"


def test_sender_history_from_the_same_company(db, t2, ticket, script, kb):
    other = intake_store.upsert(t2.get(fake.add_email(
        "Balance", "Transfer balance please", "it@alwaha-clinics.sa"))).ticket_id
    d = db.query_one("INSERT INTO drafts (ticket_id, action, product, summary, status) "
                     "VALUES (%s, 'route', 'RICH', 's', 'approved') RETURNING id", (other,))
    db.execute("INSERT INTO reviews (draft_id, reviewed_by, outcome) "
               "VALUES (%s, 'agent@t2.sa', 'approve')", (d["id"],))
    pipeline.draft(ticket)
    assert "Others at alwaha-clinics.sa:  RICH 1" in script.users["product"]


def test_snap_forgives_spacing_numbering_and_copied_descriptions():
    allowed = ["Low", "Medium", "RiCH Support", "DLR - Call Barring"]
    assert validate.snap("2. Medium", allowed) == "Medium"
    assert validate.snap(" rich support — Sender names and contracts", allowed) == "RiCH Support"
    assert validate.snap("DLR - Call Barring", allowed) == "DLR - Call Barring"
    assert validate.snap("Billing", allowed) is None
