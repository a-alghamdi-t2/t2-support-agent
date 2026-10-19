"""What the orchestrator reads.

Assembles one ticket from our copy into a single object: the ticket's own
fields, what is known about the customer, the request, and the other articles
as background.

Reads. Decides nothing. No text is altered here beyond what the cleaner already
separated.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from support_agent.config import get_settings
from support_agent.db import postgres
from support_agent.logging import get_logger

log = get_logger(__name__)

# How much of a background article to carry. They are context, not the question,
# and a long one should not crowd out the request.
BACKGROUND_CHARS = 600


@dataclass
class ArticleView:
    article_no: int | None
    sender_name: str | None
    sender_address: str | None
    subject: str | None
    body: str
    created_at: datetime | None
    attachments: list[dict] = field(default_factory=list)

    @property
    def sender(self) -> str:
        if self.sender_name and self.sender_address:
            return f"{self.sender_name} <{self.sender_address}>"
        return self.sender_address or self.sender_name or "unknown"


@dataclass
class Customer:
    email: str | None
    customer_id: str | None
    org: str | None
    status: str | None
    """Pending (a sender the ticket system does not know yet), Active, Blocked."""
    tier: str | None
    is_vip: bool

    @property
    def known(self) -> bool:
        """A customer the ticket system has on record, not just an address
        that emailed in."""
        return (self.status or "").lower() == "active"


@dataclass
class TicketContext:
    ticket_id: int
    ticket_number: str
    title: str | None
    product: str | None
    product_code: str | None
    queue: str | None
    state: str | None
    type: str | None
    priority: str | None
    sla: str | None
    subtype: str | None
    conclusions: str | None
    created_at: datetime | None
    requester_email: str
    customer: Customer
    request: ArticleView
    background: list[ArticleView]

    def render(self) -> str:

        def show(value: str | None) -> str:
            return value if value else "(empty)"

        created = "(empty)"
        if self.created_at:
            tz = get_settings().timezone
            try:
                local = self.created_at.astimezone(ZoneInfo(tz))
            except ZoneInfoNotFoundError:
                local = self.created_at
            created = local.strftime("%Y-%m-%d %H:%M") + f" ({tz})"

        lines = [
            "TICKET",
            f"  Number:      {self.ticket_number}",
            f"  Title:       {show(self.title)}",
            f"  Product:     {show(self.product)}",
            f"  Queue:       {show(self.queue)}",
            f"  State:       {show(self.state)}",
            f"  Priority:    {show(self.priority)}",
            f"  Created:     {created}",
        ]
        for label, value in (("Type", self.type), ("SubType", self.subtype),
                             ("Conclusions", self.conclusions)):
            if value:
                lines.append(f"  {label}:{' ' * max(1, 12 - len(label))}{value}")

        c = self.customer
        lines += ["", "CUSTOMER", f"  Email:       {c.email or self.requester_email}"]
        if c.known:
            if c.org:
                lines.append(f"  Organisation: {c.org}")
            if c.tier:
                lines.append(f"  Tier:        {c.tier}")
            if c.is_vip:
                lines.append("  VIP:         yes")
        elif (c.status or "").lower() == "blocked":
            lines.append("  Status:      Blocked in the ticket system")
        else:
            lines.append("  Status:      not yet a known customer (first contact by email)")

        lines += [
            "",
            "REQUEST",
            f"  From:    {self.request.sender}",
            f"  Subject: {show(self.request.subject)}",
        ]
        if self.request.attachments:
            names = ", ".join(a.get("filename") or "?" for a in self.request.attachments)
            lines.append(f"  Attached: {names}")
        lines += ["", self.request.body.strip() or "(empty body)"]

        if self.background:
            lines += ["", f"ALREADY ON THE TICKET ({len(self.background)})",
                      "  Context only. Not from the customer; never answer these."]
            for a in self.background:
                body = a.body.strip()
                if len(body) > BACKGROUND_CHARS:
                    body = body[:BACKGROUND_CHARS] + " ...(truncated)"
                lines += [
                    "",
                    f"  [{a.article_no}] {a.sender} — {show(a.subject)}",
                    "  " + body.replace("\n", "\n  "),
                ]

        return "\n".join(lines)


def _to_article(row: dict) -> ArticleView:
    return ArticleView(
        article_no=row["article_no"],
        sender_name=row["sender_name"],
        sender_address=row["sender_address"],
        subject=row["subject"],
        # body_clean is null only if the cleaner never ran; fall back rather
        # than hand the model an empty request.
        body=row["body_clean"] or row["body_raw"] or "",
        created_at=row["t2_created_at"],
        attachments=row["attachments"] or [],
    )


def build(ticket_id: int) -> TicketContext:
    """Assemble the context for one stored ticket. Raises if it is not there."""
    ticket = postgres.query_one(
        """
        SELECT id, ticket_number, title, product, product_code, queue, state, type,
               priority, sla, subtype, conclusions, t2_created_at, requester_email,
               customer_id, customer_org, customer_contact_status, customer_tier, is_vip
        FROM tickets WHERE id = %s
        """,
        (ticket_id,),
    )
    if ticket is None:
        raise LookupError(f"no ticket {ticket_id}")

    rows = postgres.query(
        """
        SELECT article_no, party, sender_name, sender_address, subject,
               body_raw, body_clean, attachments, t2_created_at
        FROM articles
        WHERE ticket_id = %s
        ORDER BY article_no NULLS LAST, id
        """,
        (ticket_id,),
    )
    request_rows = [r for r in rows if r["party"] == "request"]
    if not request_rows:
        raise LookupError(f"ticket {ticket_id} has no request article")

    context = TicketContext(
        ticket_id=ticket["id"],
        ticket_number=ticket["ticket_number"],
        title=ticket["title"],
        product=ticket["product"],
        product_code=ticket["product_code"],
        queue=ticket["queue"],
        state=ticket["state"],
        type=ticket["type"],
        priority=ticket["priority"],
        sla=ticket["sla"],
        subtype=ticket["subtype"],
        conclusions=ticket["conclusions"],
        created_at=ticket["t2_created_at"],
        requester_email=ticket["requester_email"],
        customer=Customer(
            email=ticket["requester_email"],
            customer_id=ticket["customer_id"],
            org=ticket["customer_org"],
            status=ticket["customer_contact_status"],
            tier=ticket["customer_tier"],
            is_vip=ticket["is_vip"],
        ),
        request=_to_article(request_rows[0]),
        background=[_to_article(r) for r in rows if r["party"] != "request"],
    )
    log.debug("context built", extra={"ticket": context.ticket_number,
                                      "background": len(context.background),
                                      "chars": len(context.request.body)})
    return context
