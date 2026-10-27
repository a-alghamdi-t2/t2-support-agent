"""The T2 Ticketing System, reached through its TicketAPI only."""

from support_agent.ticketsystem.client import T2Client, T2Error, connect
from support_agent.ticketsystem.models import (
    Article, Attachment, HistoryEntry, Lookups, Note, ProductValues, SpamResult, Subtype,
    Ticket, WriteResult,
)

__all__ = [
    "Article", "Attachment", "HistoryEntry", "Lookups", "Note", "ProductValues", "SpamResult",
    "Subtype", "T2Client", "T2Error", "Ticket", "WriteResult", "connect",
]
