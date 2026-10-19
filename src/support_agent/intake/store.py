"""Writing a ticket from the ticket system into our copy.

Idempotent by their identifiers: storing the same ticket again updates its
fields and adds only the articles we have not seen. Nothing here decides
anything.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, asdict

from support_agent.db import postgres
from support_agent.intake import search_text
from support_agent.intake.cleaner import clean as clean_body
from support_agent.logging import get_logger
from support_agent.ticketsystem import Article, Ticket

log = get_logger(__name__)


@dataclass
class StoredTicket:
    ticket_id: int
    ticket_number: str
    articles_inserted: int
    articles_skipped: int
    is_new: bool


def split_sender(raw: str) -> tuple[str | None, str | None]:
    """(display name, address) from a From value: their email tickets carry a
    bare address, mail headers carry "Name <address>"."""
    raw = (raw or "").strip()
    if not raw:
        return None, None
    if "<" in raw and ">" in raw:
        name = raw[: raw.index("<")].strip().strip('"') or None
        address = raw[raw.index("<") + 1: raw.index(">")].strip().lower() or None
        return name, address
    return None, raw.lower()


def _attachments(article: Article) -> str:
    return json.dumps([asdict(a) for a in article.attachments], ensure_ascii=False)


def upsert(ticket: Ticket) -> StoredTicket:
    first = ticket.first_article
    requester = ticket.customer_email or (split_sender(first.sender)[1] if first else None) or ""

    with postgres.connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM tickets WHERE t2_ticket_id = %s", (ticket.id,))
            is_new = cur.fetchone() is None

            cur.execute(
                """
                INSERT INTO tickets (
                    t2_ticket_id, ticket_number, title, product, product_code, queue,
                    state, state_type, type, priority, sla, subtype, conclusions,
                    requires_classification, requester_email, customer_id,
                    customer_contact_status, customer_org, customer_tier, is_vip,
                    t2_created_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                          %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (t2_ticket_id) DO UPDATE SET
                    title = EXCLUDED.title,
                    product = EXCLUDED.product,
                    product_code = EXCLUDED.product_code,
                    queue = EXCLUDED.queue,
                    state = EXCLUDED.state,
                    state_type = EXCLUDED.state_type,
                    type = EXCLUDED.type,
                    priority = EXCLUDED.priority,
                    sla = EXCLUDED.sla,
                    subtype = EXCLUDED.subtype,
                    conclusions = EXCLUDED.conclusions,
                    requires_classification = EXCLUDED.requires_classification,
                    customer_contact_status = EXCLUDED.customer_contact_status,
                    customer_org = EXCLUDED.customer_org,
                    customer_tier = EXCLUDED.customer_tier,
                    is_vip = EXCLUDED.is_vip,
                    last_synced_at = now()
                RETURNING id
                """,
                (
                    ticket.id, ticket.number, ticket.title or None, ticket.product or None,
                    ticket.product_code or None, ticket.queue or None, ticket.state or None,
                    ticket.state_type or None, ticket.type or None, ticket.priority or None,
                    ticket.sla or None, ticket.subtype or None, ticket.conclusions or None,
                    ticket.requires_classification, requester, ticket.customer_id or None,
                    ticket.customer_status or None, ticket.customer_org or None,
                    ticket.customer_tier or None, ticket.is_vip, ticket.created,
                ),
            )
            ticket_id = cur.fetchone()["id"]

            inserted = skipped = 0
            request_is_new = False
            for article in ticket.articles:
                is_request = first is not None and article.id == first.id
                sender_name, sender_address = split_sender(article.sender)
                parts = clean_body(article.body)
                cur.execute(
                    """
                    INSERT INTO articles (
                        ticket_id, t2_article_id, article_no, party, via,
                        sender_name, sender_address, recipients, cc, subject,
                        body_raw, body_clean, body_quoted, body_signature,
                        attachments, t2_created_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                              %s, %s, %s, %s, %s::jsonb, %s)
                    ON CONFLICT (t2_article_id) DO NOTHING
                    RETURNING id
                    """,
                    (
                        ticket_id, article.id, article.number,
                        "request" if is_request else "other",
                        "Email" if article.via_email else "T2",
                        sender_name, sender_address, article.to or None, article.cc or None,
                        article.subject or None,
                        article.body, parts.clean, parts.quoted, parts.signature,
                        _attachments(article), article.created,
                    ),
                )
                if cur.fetchone() is None:
                    skipped += 1
                else:
                    inserted += 1
                    request_is_new = request_is_new or is_request

    # After the commit, not inside it: the text is derived, and a failure to
    # build it must never lose the ticket. fill_ticket does not raise.
    if request_is_new:
        search_text.fill_ticket(ticket_id)

    log.info("ticket stored", extra={"ticket": ticket.number, "new": is_new,
                                     "articles_in": inserted, "articles_known": skipped})
    return StoredTicket(ticket_id, ticket.number, inserted, skipped, is_new)
