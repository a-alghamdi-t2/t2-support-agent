"""A closed ticket, stored for the agent to learn from.

Separate from `tickets` on purpose: that is what the agent is working on now;
this is history, read once when the ticket closes and then read many times by
what is built on top of it (the timeline, the journey, the similar-tickets and
spam indexes, the sender history in step 1).

Everything comes from one TicketGet. Idempotent by the ticket's ID, so a run
that stopped half-way can be run again: a ticket stored twice is updated, its
messages are not added twice.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime

from support_agent.config import get_settings
from support_agent.db import postgres
from support_agent.intake.cleaner import clean as split_body
from support_agent.intake.store import split_sender
from support_agent.logging import get_logger
from support_agent.ticketsystem import Article, Ticket

log = get_logger(__name__)


@dataclass
class StoredClosed:
    id: int
    ticket_number: str
    kept: int
    """Messages stored."""
    dropped: int
    """Messages left out: our own internal notes, and their page's reassign
    notes (see is_noise)."""
    spam: bool


REASSIGN_NOTE = "ticket re-assigned. justification:"
"""How their page's own note starts when someone reassigns a ticket
(Tickets/Details OnPostReassign). The same reason is on the move in the
history, with its team."""


def is_noise(article: Article) -> bool:
    """True for an internal note that is not somebody writing about the
    problem:

      ours       the notes the agent writes through the API. They repeat the
                 classification and its reasoning, which the history has from
                 the ticket's fields and moves; a journey read from them would
                 describe the agent, not how the team handled the ticket.
                 Known by their sender (AGENT_NAME): their TicketGet reports
                 every staff article as "agent", ours too, never "T2Agent".
      reassign   their page's "Ticket re-assigned. Justification: ..." note,
                 which repeats the move's reason.

    Replies are always kept, the agent's too: the customer read them."""
    if article.visible:
        return False
    if article.sender_type.lower() == "t2agent":
        return True
    sender = (article.sender or "").strip().lower()
    if sender and sender == get_settings().agent_name.strip().lower():
        return True
    return (article.body or "").strip().lower().startswith(REASSIGN_NOTE)


def closed_at(ticket: Ticket) -> datetime | None:
    """When the ticket last went to the status it is closed in, from its
    history. Their TicketGet has no close time; without a matching entry, the
    ticket's last change."""
    for h in reversed(ticket.history):
        if h.to_state and h.to_state.lower() == ticket.state.lower():
            return h.at
        if h.action.lower() in ("spam ticket closed", "pending contact blocked"):
            return h.at
    return ticket.changed


def is_spam(ticket: Ticket) -> bool:
    """Closed because its sender was marked as spam: the contact is blocked.
    Their Mark as Spam (and ContactMarkSpam) blocks the contact and closes
    every open ticket of theirs."""
    return ticket.customer_status.lower() == "blocked"


def save(ticket: Ticket) -> StoredClosed:
    """Write one closed ticket and the messages somebody wrote on it."""
    first = ticket.first_article
    requester = (ticket.customer_email
                 or (split_sender(first.sender)[1] if first else None) or "").lower() or None
    closed = closed_at(ticket)
    minutes = (int((closed - ticket.created).total_seconds() // 60)
               if closed and ticket.created else None)
    real = [a for a in ticket.articles if not is_noise(a)]
    history = [{"at": h.at.isoformat() if h.at else None, "action": h.action,
                "details": h.details, "by": h.by, "from_queue": h.from_queue,
                "to_queue": h.to_queue, "from_state": h.from_state, "to_state": h.to_state,
                "reason": h.reason, "team": h.team, "sequence": h.sequence}
               for h in ticket.history]

    with postgres.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO closed_tickets (
                    t2_ticket_id, ticket_number, title, product, product_code, queue,
                    state, type, priority, sla, subtype, conclusions, requester_email,
                    customer_id, customer_org, contact_status, t2_created_at, t2_closed_at,
                    solution_minutes, article_count, noise_count, dynamic_fields, history
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                          %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb)
                ON CONFLICT (t2_ticket_id) DO UPDATE SET
                    title = EXCLUDED.title, product = EXCLUDED.product,
                    product_code = EXCLUDED.product_code, queue = EXCLUDED.queue,
                    state = EXCLUDED.state, type = EXCLUDED.type,
                    priority = EXCLUDED.priority, sla = EXCLUDED.sla,
                    subtype = EXCLUDED.subtype, conclusions = EXCLUDED.conclusions,
                    contact_status = EXCLUDED.contact_status,
                    customer_org = EXCLUDED.customer_org,
                    t2_closed_at = EXCLUDED.t2_closed_at,
                    solution_minutes = EXCLUDED.solution_minutes,
                    article_count = EXCLUDED.article_count,
                    noise_count = EXCLUDED.noise_count,
                    dynamic_fields = EXCLUDED.dynamic_fields, history = EXCLUDED.history,
                    fetched_at = now()
                RETURNING id
                """,
                (ticket.id, ticket.number, ticket.title, ticket.product,
                 ticket.product_code or None, ticket.queue or None, ticket.state,
                 ticket.type or None, ticket.priority or None, ticket.sla or None,
                 ticket.subtype or None, ticket.conclusions or None, requester,
                 ticket.customer_id or None, ticket.customer_org or None,
                 ticket.customer_status or None, ticket.created, closed, minutes,
                 len(ticket.articles), len(ticket.articles) - len(real),
                 json.dumps(ticket.dynamic_fields, ensure_ascii=False),
                 json.dumps(history, ensure_ascii=False)))
            cid = cur.fetchone()["id"]

            for a in real:
                name, address = split_sender(a.sender)
                parts = split_body(a.body or "")
                cur.execute(
                    """
                    INSERT INTO closed_articles (
                        closed_ticket_id, t2_article_id, article_no, sender_type, channel_id,
                        visible, sender_name, sender_address, recipients, cc, subject,
                        body_raw, body_clean, body_quoted, body_signature, attachments,
                        t2_created_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                              %s::jsonb, %s)
                    ON CONFLICT (t2_article_id) DO NOTHING
                    """,
                    (cid, a.id, a.number, a.sender_type or None,
                     "email" if a.via_email else "other", a.visible, name, address,
                     a.to or None, a.cc or None, a.subject or None, a.body or "",
                     parts.clean, parts.quoted, parts.signature,
                     json.dumps([asdict(x) for x in a.attachments], ensure_ascii=False),
                     a.created))

    return StoredClosed(cid, ticket.number, len(real), len(ticket.articles) - len(real),
                        is_spam(ticket))


def history_products() -> list[str]:
    """The products whose closed tickets are learned from, and the intake's:
    spam is closed while still unclassified."""
    s = get_settings()
    return list(dict.fromkeys([*s.history_product_list, s.intake_product.upper()]))
