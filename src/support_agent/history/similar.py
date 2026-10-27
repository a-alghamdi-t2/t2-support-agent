"""Closed tickets like the one being drafted: how the team handled it before.

An index of closed tickets (PRECEDENT_TENANT), separate from the products'
knowledge bases: not what the manual says, but how this was handled the last
few times. Each entry is the customer's first message, cleaned the same way as
a new ticket's (intake.search_text), so both sides are compared on the problem
rather than on shared signatures and disclaimers. Spam stays out: a precedent
is how the team handled a real request.

When a draft is made, its ticket's search text is searched against the index
and the closest closed tickets are stored with the draft (draft_similar), with
how each one went: the queues it passed through and its journey. The reviewer
sees them in draft.py, in the note, and later in the panel. The agent does not
search them itself.

Stored, not looked up on demand: what the reviewer saw next to the decision is
what the record keeps, even after the index changes.

Nothing here may stop a draft. Every entry point catches its own failures: a
draft without similar tickets is still a draft.
"""

from __future__ import annotations

from support_agent.config import get_settings
from support_agent.db import postgres
from support_agent.history import index_sync
from support_agent.logging import get_logger

log = get_logger(__name__)

DOC_ID = "closed-tickets"
MESSAGE_CHARS = 3000
"""Of each ticket's first message: a precedent's detail is often in the middle."""
MIN_TEXT = 15
"""Shorter than this is not a message to compare on."""


def tenant() -> str:
    return get_settings().precedent_tenant


def wanted() -> dict[str, dict]:
    """What the index should hold: every real closed ticket of the history
    products with something to compare on."""
    rows = postgres.query(
        """
        SELECT id, ticket_number, product_code, queue, type, subtype, search_text
        FROM closed_tickets
        WHERE length(coalesce(search_text, '')) >= %s
          AND coalesce(contact_status, '') <> 'Blocked'
          AND upper(coalesce(product_code, '')) = ANY(%s)
        """, (MIN_TEXT, get_settings().history_product_list))
    return {r["ticket_number"]: index_sync.entry(
        r["search_text"][:MESSAGE_CHARS], r["ticket_number"], DOC_ID,
        closed_ticket_id=r["id"], product=r["product_code"], queue=r["queue"],
        type=r["type"], subtype=r["subtype"]) for r in rows}


def sync(refresh: set[str] | None = None) -> dict:
    return index_sync.sync(tenant(), DOC_ID, wanted(), refresh)


def find(text: str, *, limit: int | None = None, exclude: str | None = None) -> list[dict]:
    """The closest closed tickets to this text, best first, at or above
    SIMILAR_MIN_SCORE. Raises when the search fails."""
    s = get_settings()
    limit = limit or s.similar_limit
    if not (text or "").strip():
        return []
    hits = [h for h in index_sync.search(tenant(), text, limit + (1 if exclude else 0))
            if h["ticket_number"] != exclude]
    # When nothing is really alike the search still returns its least-bad
    # matches, scoring near zero: those are not precedents.
    hits = [h for h in hits if h["score"] >= s.similar_min_score][:limit]
    if not hits:
        return []
    rows = {r["ticket_number"]: r for r in postgres.query(
        "SELECT id, ticket_number FROM closed_tickets WHERE ticket_number = ANY(%s)",
        ([h["ticket_number"] for h in hits],))}
    return [{"id": rows[h["ticket_number"]]["id"], "ticket_number": h["ticket_number"],
             "rank": i, "score": h["score"]}
            for i, h in enumerate((h for h in hits if h["ticket_number"] in rows), 1)]


def _mark(draft_id: int, status: str) -> None:
    postgres.execute("UPDATE drafts SET similar_status = %s WHERE id = %s", (status, draft_id))


def attach(draft_id: int) -> int:
    """Find and store the similar tickets for a new draft. Returns how many.
    Records what happened on the draft (found / none / no_index / failed), so
    "the search failed" is not shown as "nothing similar". Never raises."""
    try:
        row = postgres.query_one(
            "SELECT t.search_text FROM drafts d JOIN tickets t ON t.id = d.ticket_id "
            "WHERE d.id = %s", (draft_id,))
        text = (row or {}).get("search_text") or ""
        if not text.strip():
            _mark(draft_id, "none")
            return 0
        if index_sync.size(tenant()) == 0:
            _mark(draft_id, "no_index")
            return 0
        try:
            matches = find(text)
        except Exception:
            log.exception("similar search failed", extra={"draft": draft_id})
            _mark(draft_id, "failed")
            return 0
        for m in matches:
            postgres.execute(
                "INSERT INTO draft_similar (draft_id, closed_ticket_id, rank, score) "
                "VALUES (%s, %s, %s, %s) ON CONFLICT (draft_id, closed_ticket_id) DO NOTHING",
                (draft_id, m["id"], m["rank"], m["score"]))
        _mark(draft_id, "found" if matches else "none")
        return len(matches)
    except Exception:
        log.exception("similar tickets failed", extra={"draft": draft_id})
        try:
            _mark(draft_id, "failed")
        except Exception:
            pass
        return 0


def for_draft(draft_id: int) -> list[dict]:
    """The stored matches for one draft, with how each ticket went."""
    try:
        return postgres.query(
            """
            SELECT s.rank, s.score, c.ticket_number, c.title, c.product_code,
                   coalesce(c.product, c.product_code) AS product_name, c.queue,
                   c.type, c.subtype, c.t2_closed_at, left(c.search_text, 600) AS text,
                   coalesce(tl.queue_path, '{}') AS path, j.summary AS journey,
                   coalesce(j.moves, '[]'::jsonb) AS moves
            FROM draft_similar s
            JOIN closed_tickets c ON c.id = s.closed_ticket_id
            LEFT JOIN ticket_timelines tl ON tl.closed_ticket_id = c.id
            LEFT JOIN ticket_journeys j ON j.closed_ticket_id = c.id
            WHERE s.draft_id = %s ORDER BY s.rank
            """, (draft_id,))
    except Exception:
        log.exception("could not read similar tickets", extra={"draft": draft_id})
        return []
