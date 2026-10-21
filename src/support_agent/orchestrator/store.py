"""Storing a decision as a draft.

One pending draft per ticket, enforced by a partial unique index rather than by
code. Writing a new one supersedes any pending one first, with a conditional
update: if a reviewer approved it in the meantime the update touches nothing,
and the approval stands.

Every search the agent ran is kept in the evidence column, not just the one
that decided it -- a search that found nothing is part of why we routed. What
the agent did, step by step, is kept in the trace column. Neither goes in the
audit log, which records only what people do.
"""

from __future__ import annotations

import json
from typing import Any

from support_agent.config import get_settings
from support_agent.db import postgres
from support_agent.knowledge.contracts import Retrieval, strongest
from support_agent.logging import get_logger

log = get_logger(__name__)

COLUMNS = ("action", "reply_kind", "reply_body", "product", "product_sure", "queue",
           "queue_reason", "next_state", "type", "subtype", "conclusions", "priority",
           "issue_type", "summary", "collected", "missing", "note", "confidence",
           "reasoning", "model", "prompt_ver", "latency_ms", "replaces_draft_id")


def evidence_json(retrievals: list[Retrieval], corrections: list[str],
                  held_reply: dict | None = None) -> dict:
    s = get_settings()
    return {
        "verdict": strongest(retrievals).value if retrievals else "not_in_kb",
        "searches": [r.as_json() for r in retrievals],
        "corrections": corrections,
        # A reply the agent wrote on a draft that routes. Not sent anywhere:
        # the review panel offers it when the reviewer changes it to an answer.
        "held_reply": held_reply,
        # The settings that produced this: a draft made at pool 10 is not
        # comparable with one made at 30.
        "settings": {"max_rounds": s.kb_max_rounds, "candidate_pool": s.kb_candidate_pool,
                     "top_k": s.kb_top_k},
    }


def save(ticket_id: int, values: dict[str, Any], *, evidence: dict, trace: list,
         urgent_reason: str | None = None) -> int:
    """Write the draft. Returns its id."""
    unknown = set(values) - set(COLUMNS)
    if unknown:
        raise ValueError(f"not draft columns: {sorted(unknown)}")
    row = {k: values.get(k) for k in COLUMNS}
    row["collected"] = json.dumps(row["collected"] or {}, ensure_ascii=False)
    row["missing"] = json.dumps(row["missing"] or [], ensure_ascii=False)
    if row["product_sure"] is None:
        row["product_sure"] = True

    built_to = postgres.query_one(
        "SELECT id FROM articles WHERE ticket_id = %s "
        "ORDER BY article_no DESC NULLS LAST, id DESC LIMIT 1", (ticket_id,))

    with postgres.connection() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE drafts SET status = 'superseded' "
                        "WHERE ticket_id = %s AND status = 'pending'", (ticket_id,))
            superseded = cur.rowcount
            cols = ", ".join(COLUMNS)
            marks = ", ".join(f"%({c})s" for c in COLUMNS)
            cur.execute(
                f"""
                INSERT INTO drafts (ticket_id, status, {cols}, evidence, trace,
                                    built_to_article_id)
                VALUES (%(ticket_id)s, 'pending', {marks}, %(evidence)s::jsonb,
                        %(trace)s::jsonb, %(built_to)s)
                RETURNING id
                """,
                {**row, "ticket_id": ticket_id,
                 "evidence": json.dumps(evidence, ensure_ascii=False, default=str),
                 "trace": json.dumps(trace, ensure_ascii=False, default=str),
                 "built_to": built_to["id"] if built_to else None})
            draft_id = cur.fetchone()["id"]
            if urgent_reason:
                cur.execute("INSERT INTO draft_urgent (draft_id, by_agent, agent_reason) "
                            "VALUES (%s, true, %s)", (draft_id, urgent_reason))

    log.info("draft saved", extra={"ticket": ticket_id, "draft": draft_id,
                                   "action": row["action"], "product": row["product"],
                                   "superseded": superseded})
    return draft_id
