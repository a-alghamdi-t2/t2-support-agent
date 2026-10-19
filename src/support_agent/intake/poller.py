"""Check the intake, store what is waiting, and hand it on to be drafted.

The intake is every email ticket the ticket system holds as "Unclassified
Email" with no queue and nobody assigned, still marked for classification.
Nothing here writes to the ticket system: a check stores tickets and makes
drafts, and a draft reaches the ticket only when a person approves it.

Idempotent by their identifiers. A ticket already drafted is skipped rather
than drafted again -- checking is cheap, drafting is a minute of model time.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from support_agent.db import postgres
from support_agent.intake import store as intake_store
from support_agent.logging import get_logger
from support_agent.ticketsystem import T2Client, T2Error, Ticket, connect

log = get_logger(__name__)


@dataclass
class PollResult:
    seen: int = 0
    drafted: int = 0
    skipped: int = 0
    failed: int = 0
    withdrawn: int = 0
    seconds: float = 0.0
    tickets: list[str] = field(default_factory=list)


def left_intake(ticket: Ticket, intake_product: str) -> str | None:
    """Why a ticket is no longer the agent's to draft, or None if it still is."""
    if ticket.is_closed:
        return f"closed ({ticket.state})"
    if ticket.product_code.upper() != intake_product.upper():
        return f"given the product {ticket.product}"
    if ticket.queue:
        return f"put in {ticket.queue}"
    if not ticket.requires_classification:
        return "classified"
    return None


def _already_drafted(ticket_id: int, newest_article_id: int | None) -> bool:
    """True when a draft already covers this ticket as it stands.

    A draft is stale once an article arrives after the one it was built from:
    the customer wrote again, and the draft may no longer fit. Then the
    ticket is drafted again and the old draft is superseded.

    A rejected or approved draft counts as covering the ticket: a rejection is
    not a reason to draft again, a new article is. Otherwise every check
    after a rejection would draft the same ticket once more.
    """
    row = postgres.query_one(
        "SELECT built_to_article_id FROM drafts WHERE ticket_id = %s ORDER BY id DESC LIMIT 1",
        (ticket_id,))
    if row is None:
        return False
    if newest_article_id is None:
        return True
    built_to = row["built_to_article_id"]
    return built_to is not None and built_to >= newest_article_id


def _newest_article(ticket_id: int) -> int | None:
    row = postgres.query_one(
        """
        SELECT id FROM articles WHERE ticket_id = %s
        ORDER BY article_no DESC NULLS LAST, id DESC LIMIT 1
        """,
        (ticket_id,))
    return row["id"] if row else None


def _draft(ticket_id: int) -> int | None:
    """Make the draft for one stored ticket: the orchestrator and the checks
    after it. Returns the draft's id, or None when it could not be made."""
    from support_agent.orchestrator import pipeline     # the agent; heavy to import

    return pipeline.draft(ticket_id)


def poll_once(*, decide: bool = True, t2: T2Client | None = None) -> PollResult:
    """One pass over the intake.

    With decide=False the tickets are stored and nothing is sent to the model:
    for filling the database quickly, or while the knowledge base is down.
    """
    started = time.perf_counter()
    result = PollResult()
    own = t2 is None
    t2 = t2 or connect()
    try:
        try:
            ids = t2.intake()
        except T2Error as exc:
            log.error("intake check failed", extra={"error": str(exc)})
            return result
        result.seen = len(ids)
        log.info("intake checked", extra={"waiting": len(ids)})

        # Tickets someone handled on their screen while the draft waited.
        # Only when the list is complete: a capped list cannot tell "taken"
        # from "not listed".
        if len(ids) < t2.intake_limit:
            result.withdrawn = withdraw_taken(t2, set(ids))

        for t2_id in ids:
            try:
                ticket = t2.get(t2_id)
            except T2Error as exc:
                log.error("could not read ticket", extra={"ticket": t2_id, "error": str(exc)})
                result.failed += 1
                continue
            if left_intake(ticket, t2.intake_product):
                result.skipped += 1           # changed between the list and the read
                continue

            stored = intake_store.upsert(ticket)
            result.tickets.append(stored.ticket_number)
            if not decide:
                result.skipped += 1
                continue
            if _already_drafted(stored.ticket_id, _newest_article(stored.ticket_id)):
                result.skipped += 1
                continue
            try:
                draft_id = _draft(stored.ticket_id)
            except Exception:
                log.exception("drafting failed", extra={"ticket": stored.ticket_number})
                draft_id = None
            if draft_id is None:
                result.failed += 1
            else:
                result.drafted += 1
    finally:
        if own:
            t2.close()
        result.seconds = time.perf_counter() - started
    return result


def withdraw_taken(t2: T2Client, waiting: set[str]) -> int:
    """Withdraw pending drafts whose ticket a person already handled.

    Someone may give the ticket a product or queue, classify it or close it on
    their screen after the agent drafted it. Approving the draft would then
    write over what they did. So each pass looks at the pending drafts whose
    ticket is no longer in the intake, and withdraws those that left it: the
    draft leaves the review list, and the ticket's row shows where it went.
    A ticket that is only in another status (a reply was sent, say) is still
    the agent's, and its draft stays.
    """
    rows = postgres.query(
        """
        SELECT d.id AS draft_id, t.id AS ticket_id, t.t2_ticket_id, t.ticket_number
        FROM drafts d JOIN tickets t ON t.id = d.ticket_id
        WHERE d.status = 'pending'
        """)
    withdrawn = 0
    for r in rows:
        if r["t2_ticket_id"] in waiting:
            continue
        try:
            ticket = t2.get(r["t2_ticket_id"], articles=False)
        except T2Error as exc:
            log.warning("could not check a pending draft's ticket",
                        extra={"ticket": r["ticket_number"], "error": str(exc)[:200]})
            continue
        why = left_intake(ticket, t2.intake_product)
        if why is None:
            continue
        changed = postgres.execute(
            "UPDATE drafts SET status = 'superseded' WHERE id = %s AND status = 'pending'",
            (r["draft_id"],))
        if not changed:
            continue          # reviewed in the meantime
        postgres.execute(
            """
            UPDATE tickets SET product = %s, product_code = %s, queue = %s, state = %s,
                   state_type = %s, requires_classification = %s, last_synced_at = now()
            WHERE id = %s
            """,
            (ticket.product or None, ticket.product_code or None, ticket.queue or None,
             ticket.state or None, ticket.state_type or None, ticket.requires_classification,
             r["ticket_id"]))
        log.info("draft withdrawn", extra={"ticket": r["ticket_number"], "why": why})
        withdrawn += 1
    return withdrawn
