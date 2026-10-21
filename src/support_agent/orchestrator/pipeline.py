"""One ticket in, one draft out.

    1. product   which product, or spam             (orchestrator.product)
    2. route     answer or route, queue, type,      (orchestrator.route)
                 status, priority, reply; searches
                 only the chosen product's documents
    3. subtype   among the chosen type's subtypes;  (orchestrator.subtype)
                 Conclusions is its reason

Spam stops after step 1. So does a product with no queues yet: there is
nothing to route to, and the reviewer routes it by hand.

draft(ticket_id, product=CODE, replaces=ID) is drafting again for a product a
reviewer chose: step 1 is skipped, and the new draft points at the one it
replaces, so both are kept.
"""

from __future__ import annotations

import time

from support_agent import llm, options
from support_agent.config import get_settings
from support_agent.intake import context as ticket_context
from support_agent.logging import get_logger
from support_agent.orchestrator import product as product_step
from support_agent.orchestrator import route as route_step
from support_agent.orchestrator import store
from support_agent.orchestrator import subtype as subtype_step
from support_agent.orchestrator.contracts import Action

log = get_logger(__name__)

PROMPT_VER = "t2-v1"


class DecisionFailed(RuntimeError):
    """No draft could be made. Carries what the agent did before failing."""

    def __init__(self, message: str, trace: list | None = None):
        super().__init__(message)
        self.trace = trace or []


def draft(ticket_id: int, *, product: str | None = None,
          replaces: int | None = None) -> int:
    """Make the draft for one stored ticket. Returns its id; raises
    DecisionFailed when none could be made (the poller tries again on its
    next pass)."""
    started = time.perf_counter()
    options.sync()                      # skipped when the values are fresh
    context = ticket_context.build(ticket_id)
    s = get_settings()
    trace: list = [{"step": "started", "ticket": context.ticket_number,
                    "model": s.groq_model, "prompt_ver": PROMPT_VER,
                    "redraft_for": product, "replaces": replaces}]
    base = {"model": s.groq_model, "prompt_ver": PROMPT_VER, "replaces_draft_id": replaces}

    try:
        if product is None:
            choice, chosen = product_step.run(context, trace)
            code, name, sure = chosen.code, chosen.name, chosen.sure
            why, summary = choice.reason, choice.summary
            corrections = list(chosen.corrections)
            if chosen.spam:
                values = {**base, "action": Action.SPAM.value, "product": code,
                          "product_sure": sure, "summary": summary,
                          "reasoning": choice.spam_reason or choice.reason}
                return _save(ticket_id, values, started, trace, corrections=corrections)
        else:
            o = options.product(product)
            if o is None or o.removed:
                raise DecisionFailed(f"no product {product!r}", trace)
            code, name, sure, why, summary = o.product, o.value, True, None, None
            corrections = []

        if not options.queues(code):
            trace.append({"step": "route", "skipped": f"{name} has no queues"})
            values = {**base, "action": Action.ROUTE.value, "product": code,
                      "product_sure": sure, "summary": summary or context.title or "(none)",
                      "reasoning": why,
                      "note": f"{name} has no queues in the ticket system yet: "
                              "choose where it goes by hand."}
            return _save(ticket_id, values, started, trace, corrections=corrections)

        decision, retrievals, more = route_step.run(context, code, name, why, trace)
        corrections += more
        sub, conclusions, more = subtype_step.run(context, code, decision, trace)
        corrections += more
    except llm.LLMError as exc:
        trace.append({"step": "failed", "error": str(exc)[:500]})
        log.error("no draft", extra={"ticket": context.ticket_number, "error": str(exc)[:300]})
        raise DecisionFailed(str(exc), trace) from exc

    values = {
        **base,
        "action": decision.action.value,
        "reply_kind": decision.reply_kind.value if decision.reply_kind else None,
        "reply_body": decision.reply_body,
        "product": code, "product_sure": sure,
        "queue": decision.queue, "queue_reason": decision.queue_reason,
        "next_state": decision.next_state, "type": decision.type,
        "subtype": sub, "conclusions": conclusions, "priority": decision.priority,
        "issue_type": decision.issue_type, "summary": decision.summary,
        "collected": decision.collected, "missing": decision.missing,
        "note": decision.note, "confidence": decision.confidence.value,
        "reasoning": decision.reasoning,
    }
    return _save(ticket_id, values, started, trace, corrections=corrections,
                 retrievals=retrievals, held_reply=decision._held_reply,
                 urgent_reason=(decision.urgent_reason or "urgent") if decision.urgent
                 else None)


def _save(ticket_id: int, values: dict, started: float, trace: list, *,
          corrections: list[str], retrievals=None, held_reply=None,
          urgent_reason=None) -> int:
    values["latency_ms"] = int((time.perf_counter() - started) * 1000)
    trace.append({"step": "done", "ms": values["latency_ms"],
                  "tokens": _tokens(trace)})
    return store.save(ticket_id, values,
                      evidence=store.evidence_json(retrievals or [], corrections, held_reply),
                      trace=trace, urgent_reason=urgent_reason)


def _tokens(trace: list) -> dict[str, int]:
    total = {"prompt": 0, "completion": 0}
    for t in trace:
        for k, v in (t.get("tokens") or {}).items():
            if k in total:
                total[k] += v
    return total
