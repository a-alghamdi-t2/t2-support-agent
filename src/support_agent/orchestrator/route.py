"""Step 2: within the chosen product, answer or route, and the fields.

The model gets the ticket and one tool, the product's own knowledge base. It
reads the ticket, works out what is actually being asked -- a body is a
forwarded chain with signatures and pleasantries, and the question is somewhere
inside it -- and searches in its own words. Up to four searches, because a
ticket can ask more than one thing.

Deciding is a second call rather than the same one: tool calling and JSON mode
together are unreliable, so the search loop finishes first and the
conversation is then asked for the decision.
"""

from __future__ import annotations

import json
import time
from functools import lru_cache

from support_agent import llm, options
from support_agent.config import get_settings
from support_agent.db import postgres
from support_agent.intake.context import TicketContext
from support_agent.knowledge import retriever
from support_agent.knowledge.contracts import Retrieval
from support_agent.logging import get_logger
from support_agent.orchestrator import validate
from support_agent.orchestrator.contracts import Decision

log = get_logger(__name__)

MAX_KB_CALLS = 4
"""A cap, so a bad prompt cannot loop. Four because a ticket can ask several
things, and each needs its own search."""

PASSAGE_CHARS = 1200
"""How much of a passage to hand back to the model. Enough to write a reply
from, short enough that four searches do not fill the context."""

TOOL = "search_knowledge_base"


@lru_cache(maxsize=1)
def _base_prompt() -> str:
    return (get_settings().prompts_dir / "orchestrator" / "route.md").read_text(
        encoding="utf-8")


def _product_prompt(code: str) -> str:
    """products/<code>/route.md, when the product has something of its own to
    add. Read every time: it is small, and an edit counts at once."""
    path = get_settings().product_dir(code) / "route.md"
    return path.read_text(encoding="utf-8").strip() if path.is_file() else ""


def _options_block(code: str) -> str:
    """How to choose each field and the values to choose from, as they are in
    agent_options now. Built per ticket, so a description written on the
    admin page reaches the next decision."""
    how = options.guidance()

    def listed(rows) -> list[str]:
        return [f"  {o.value}" + (f" — {o.description.strip()}" if o.description else "")
                for o in rows]

    lines = ["", "## Choosing the fields", "",
             "Choose each of these from its list, exactly as written: the value, "
             "not the text after the dash."]
    for kind, rows in (("queue", options.queues(code)),
                       ("type", [t.option for t in options.types(code)]),
                       ("next_state", options.statuses()),
                       ("priority", options.priorities())):
        lines += ["", f"### {kind}"]
        if how.get(kind):
            lines += ["", how[kind].strip()]
        lines += ["", "One of:", *listed(rows)]
    return "\n".join(lines)


def _scopes_block(code: str) -> str:
    """The product's team scopes, whole: what each team handles and where the
    lines between them fall. The newest upload counts; the shared one too."""
    rows = postgres.query(
        """
        SELECT DISTINCT ON (coalesce(upper(product), '')) product, content
        FROM team_scopes
        WHERE product IS NULL OR upper(product) = %s
        ORDER BY coalesce(upper(product), ''), id DESC
        """,
        (code.upper(),))
    if not rows:
        return ""
    text = "\n\n".join(r["content"].strip() for r in rows)
    return ("\n\n## Team scopes\n\nWhat each team handles and where the lines between "
            "them fall, kept by the teams themselves. Choose the queue by this.\n\n" + text)


def system_prompt(code: str, kb_missing: str | None) -> str:
    parts = [_base_prompt()]
    if extra := _product_prompt(code):
        parts.append("## This product\n\n" + extra)
    parts.append(_options_block(code))
    if scopes := _scopes_block(code):
        parts.append(scopes)
    if kb_missing:
        parts.append(
            "\n## Knowledge base\n\nThis product's knowledge base cannot be searched "
            f"({kb_missing}). You have no documents to answer from: ask for more, or route.")
    return "\n".join(parts)


def _tool(name: str) -> dict:
    return {"type": "function", "function": {
        "name": TOOL,
        "description": (f"Search the {name} knowledge base. Ask one clear question in your "
                        "own words, not the customer's raw text. Search once per question "
                        "the ticket asks."),
        "parameters": {"type": "object", "required": ["question"], "properties": {
            "question": {"type": "string", "description": "One question, in your own words"}}},
    }}


def _search(question: str, code: str, sink: list[Retrieval], trace: list) -> str:
    result = retriever.retrieve(question, code)
    sink.append(result)
    trace.append({"step": "search", "query": question,
                  "verdict": result.verdict.value if result.verdict else None,
                  "evidence": [{"document": e.document, "section": e.section,
                                "relation": e.relation} for e in result.evidence],
                  "ms": result.elapsed_ms,
                  "error": (result.error or None) and result.error[:300]})
    if result.error:
        return json.dumps({
            "status": "search_failed", "detail": result.error[:300],
            "note": "The knowledge base could not be reached. This does NOT mean the "
                    "answer is absent. Do not treat it as not covered.",
        }, ensure_ascii=False)
    return json.dumps({
        "verdict": result.verdict.value,
        "evidence": [{"document": e.document, "section": e.section, "relation": e.relation,
                      "why": e.reason, "text": e.text[:PASSAGE_CHARS]}
                     for e in result.evidence],
    }, ensure_ascii=False)


def _call(convo: list, tools: list | None, trace: list) -> llm.Reply | None:
    for attempt in (1, 2):
        try:
            started = time.perf_counter()
            reply = llm.complete(convo, tools=tools, temperature=0.0 if attempt == 1 else 0.3)
            trace.append({"step": "route.call", "attempt": attempt,
                          "ms": int((time.perf_counter() - started) * 1000),
                          "tokens": reply.usage,
                          "searches": [c.arguments.get("question") for c in reply.tool_calls]})
            return reply
        except llm.ToolCallFailed as exc:
            trace.append({"step": "route.retry", "reason": "malformed tool call",
                          "attempt": attempt, "error": str(exc)[:300]})
    return None


def run(context: TicketContext, code: str, name: str, why: str | None, trace: list
        ) -> tuple[Decision, list[Retrieval], list[str]]:
    """The decision for one ticket within one product, checked."""
    kb_missing = retriever.unavailable(code)
    trace.append({"step": "route.input", "product": code,
                  "knowledge_base": get_settings().kb_tenant(code),
                  "kb_unavailable": kb_missing})

    user = context.render() + f"\n\nPRODUCT\n  Chosen: {name} ({code})"
    if why:
        user += f"\n  Why:    {why}"
    convo: list = [{"role": "system", "content": system_prompt(code, kb_missing)},
                   {"role": "user", "content": user}]

    retrievals: list[Retrieval] = []
    tools = None if kb_missing else [_tool(name)]
    if tools:
        searches = 0
        nudged = False
        while searches < MAX_KB_CALLS:
            reply = _call(convo, tools, trace)
            if reply is None:
                break
            convo.append(reply.as_message())
            if not reply.tool_calls:
                if searches == 0 and not nudged:
                    # Routing without searching is a shortcut the model takes
                    # when allowed. It is sometimes right -- an account question
                    # has no knowledge-base answer -- so ask rather than forbid.
                    nudged = True
                    trace.append({"step": "nudge", "reason": "no search yet"})
                    convo.append({"role": "user", "content": (
                        "You have not searched the knowledge base. If this is an account "
                        "or case-state question that no knowledge base could answer, say "
                        "so in one line. Otherwise search now.")})
                    continue
                break
            for c in reply.tool_calls:
                if c.name != TOOL or not str(c.arguments.get("question", "")).strip():
                    out = f"No tool named {c.name}." if c.name != TOOL else "No question given."
                else:
                    out = _search(str(c.arguments["question"]), code, retrievals, trace)
                    searches += 1
                convo.append({"role": "tool", "tool_call_id": c.id, "content": out})
        if searches >= MAX_KB_CALLS:
            trace.append({"step": "nudge", "reason": f"all {MAX_KB_CALLS} searches used"})
            convo.append({"role": "user", "content":
                          "You have used all your searches. Decide with what you have."})

    convo.append({"role": "user", "content": "Now give your decision for this ticket."})
    proposed = llm.ask_json(convo, Decision, trace=trace, step="route")
    trace.append({"step": "route", "proposed": proposed.model_dump(mode="json")})
    decision, corrections = validate.decision(proposed, code, retrievals)
    trace.append({"step": "route.checked", "corrections": corrections})
    log.info("routed", extra={"ticket": context.ticket_number, "product": code,
                              "action": decision.action.value, "queue": decision.queue,
                              "searches": len(retrievals), "corrections": len(corrections)})
    return decision, retrievals, corrections
