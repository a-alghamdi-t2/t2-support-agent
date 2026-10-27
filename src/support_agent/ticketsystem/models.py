"""What the T2 Ticketing System's TicketAPI returns, as plain objects.

Built from its JSON in one place (client.py), so nothing else in the agent
reads their field names. Times are timezone-aware; IDs are lower case.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

CLOSED_STATE_TYPES = ("closed", "merged")


@dataclass(frozen=True)
class Attachment:
    id: str
    filename: str
    content_type: str
    size: int | None
    url: str


@dataclass(frozen=True)
class Article:
    id: str
    number: int
    from_customer: bool
    via_email: bool
    visible: bool
    """False for an internal note."""
    sender: str
    to: str
    cc: str
    subject: str
    body: str
    created: datetime | None
    message_id: str
    attachments: tuple[Attachment, ...] = ()
    sender_type: str = ""
    """Their own word: "customer", "agent" (staff on their page), "T2Agent"
    (written through the API: our notes and replies), "system"."""

    @property
    def is_description(self) -> bool:
        """The ticket's description, which TicketGet lists as the first
        article when no message holds the same text (a ticket created on
        their screen without emailing the customer). It has no real ID."""
        return self.id.startswith("description-")


@dataclass(frozen=True)
class HistoryEntry:
    at: datetime | None
    action: str
    """Their wording: "Reassigned", "Status Changed", "Product Changed",
    "Priority Changed", "Internal Note Added", "Reply Added" ..."""
    details: str
    by: str
    from_queue: str = ""
    to_queue: str = ""
    from_state: str = ""
    to_state: str = ""
    reason: str = ""
    """The justification written when a person reassigned the ticket."""
    team: str = ""
    """The responsible team named in that justification."""
    sequence: int | None = None
    """Their order for events with the same time (a reassignment before the
    note added with it). Empty on events from before they added it; those
    come first."""

    @property
    def is_queue_move(self) -> bool:
        return bool(self.to_queue)


@dataclass(frozen=True)
class Ticket:
    id: str
    number: str
    title: str
    product: str
    product_code: str
    queue: str
    state: str
    state_type: str
    type: str
    priority: str
    sla: str
    subtype: str
    conclusions: str
    requires_classification: bool
    customer_id: str
    customer_email: str
    customer_status: str
    """Pending, Active or Blocked."""
    customer_org: str
    customer_tier: str
    is_vip: bool
    created: datetime | None
    changed: datetime | None
    dynamic_fields: dict[str, str] = field(default_factory=dict)
    articles: tuple[Article, ...] = ()
    history: tuple[HistoryEntry, ...] = ()
    assigned_staff: str = ""
    """Who the ticket is assigned to (their email, else their name), or ""
    when nobody is. As read: someone can take it a moment later."""

    @property
    def is_closed(self) -> bool:
        return self.state_type in CLOSED_STATE_TYPES

    @property
    def first_article(self) -> Article | None:
        return self.articles[0] if self.articles else None

    @property
    def queue_moves(self) -> list[HistoryEntry]:
        return [h for h in self.history if h.is_queue_move]


@dataclass(frozen=True)
class Subtype:
    name: str
    reason: str | None = None
    """The text their screen expects in Conclusions for this subtype. None
    when the product's catalog gives none (only RiCH's does today)."""


@dataclass(frozen=True)
class ProductValues:
    name: str
    code: str
    queues: tuple[str, ...]
    types: dict[str, tuple[Subtype, ...]]
    slas: tuple[str, ...]


@dataclass(frozen=True)
class Lookups:
    products: tuple[ProductValues, ...]
    statuses: tuple[tuple[str, bool], ...]
    """(name, closed) in their order."""
    priorities: tuple[str, ...]

    def product(self, name_or_code: str) -> ProductValues | None:
        key = name_or_code.strip().lower()
        for p in self.products:
            if key in (p.name.lower(), p.code.lower()):
                return p
        return None


@dataclass(frozen=True)
class Note:
    """An article the agent adds to a ticket."""

    subject: str
    body: str
    visible: bool = False
    """False: an internal note, which staff see and the customer does not.
    True: a reply, which their system emails to the customer at once, from
    the support mailbox, with the ticket number added to the subject. It is
    refused for a pending contact or an unclassified ticket."""
    sender: str | None = None
    """Who the ticket page shows as having written it (their Article.From).
    Used for internal notes; a reply always shows the support mailbox."""


@dataclass(frozen=True)
class WriteResult:
    ticket_id: str
    number: str
    article_id: str | None
    email_sent: bool | None = None
    """True when a reply was emailed, False for an internal note, None when
    no article was written."""
    message_id: str | None = None
    """The sent email's ID (Gmail's, or the SMTP Message-ID)."""


@dataclass(frozen=True)
class SpamResult:
    """ContactMarkSpam: the sender blocked and their open tickets closed."""
    ticket_id: str
    customer_id: str
    contact_status: str
    tickets_closed: int
