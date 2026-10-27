"""A closed ticket as a timeline: everything that happened on it, in order.

Built from one TicketGet: the messages, and the history's queue moves. Each
becomes one entry:

    customer   the customer writing in
    reply      our staff writing to the customer (on their page, or a reply
               the agent sent)
    note       our staff writing internally; the customer never saw it
    queue      the ticket moved to a queue: from the history's "Reassigned"
               entry, with the reason and the responsible team a person wrote
               when they moved it

Better than the old system's: there the moves had to be read out of
notification emails; here the history records each one, with its reason.

Text is cleaned per message the way search_text is: every reply quotes the one
before, so only what the person wrote is kept (cleaner.py), then signatures,
disclaimers and greetings (search_text.build). Deterministic: no model reads
anything here. This is what the journey step reads.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable

from support_agent.history.store import is_noise
from support_agent.intake.cleaner import clean as split_body
from support_agent.intake.search_text import Boilerplate, build as clean_text
from support_agent.intake.store import split_sender
from support_agent.ticketsystem import Ticket

CLEAN_VER = "timeline-t2-v1"

_EARLIEST = datetime.min.replace(tzinfo=timezone.utc)
"""For an entry with no time: their times are timezone-aware, and a naive
datetime.min cannot be compared with them."""

TEXT_CAP = 2000
"""One message's text is cut here: a pasted log should not crowd the rest of
the ticket out of what the model reads."""


@dataclass
class Timeline:
    entries: list[dict] = field(default_factory=list)
    queue_path: list[str] = field(default_factory=list)
    article_count: int = 0
    history_count: int = 0
    noise_count: int = 0
    empty_count: int = 0
    """Written, but nothing left once cleaned: a "thanks" and a signature."""


def kind_of(sender_type: str, visible: bool, from_customer: bool) -> str:
    if from_customer or sender_type.lower() == "customer":
        return "customer"
    return "reply" if visible else "note"


def path(queues: list[str]) -> list[str]:
    """Repeats in a row collapsed: moved twice into one queue is one stay."""
    route: list[str] = []
    for q in queues:
        if q and (not route or route[-1] != q):
            route.append(q)
    return route


def _drop_own_name(text: str, name: str | None, address: str | None = None) -> str:
    """Lines made only of the sender's own name: the signature "Hajar" under
    "Best regards", which the shared rules cannot know is a name. Their staff
    write with an address and no display name, so the address's local part
    ("omar.saleh@") stands in for it."""
    source = name or (address or "").split("@", 1)[0].replace(".", " ").replace("_", " ")
    words = {w.casefold() for w in re.findall(r"[^\W\d_]+", source)}
    if not words:
        return text
    kept = [line for line in text.split("\n")
            if not ((tokens := [w.casefold() for w in re.findall(r"\w+", line)])
                    and len(tokens) <= 4 and all(t in words for t in tokens))]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(kept)).strip()


def _when(at: datetime | None) -> str:
    return at.strftime("%Y-%m-%d %H:%M") if at else ""


def build(ticket: Ticket, boilerplate_for: Callable[[str | None], Boilerplate]) -> Timeline:
    """The timeline of one ticket. boilerplate_for(address) gives the learned
    lines to remove for a sender; the caller caches it."""
    tl = Timeline(article_count=len(ticket.articles))
    items: list[tuple] = []

    # Moves first at the same moment: their system writes the reassignment
    # before the note added in the same update (their history Sequence).
    moves = [h for h in ticket.history if h.to_queue]
    tl.history_count = len(moves)
    first = True
    for h in moves:
        if first and h.from_queue:
            # Where it was before the first recorded move.
            tl.queue_path.append(h.from_queue)
        first = False
        tl.queue_path.append(h.to_queue)
        items.append((h.at or _EARLIEST, 0, h.sequence or 0, {
            "at": _when(h.at), "kind": "queue", "queue": h.to_queue,
            "from_queue": h.from_queue or None, "reason": h.reason or None,
            "team": h.team or None}))

    first_message = True
    for a in sorted(ticket.articles, key=lambda a: (a.number, a.created or _EARLIEST)):
        if is_noise(a):
            tl.noise_count += 1
            continue
        kind = kind_of(a.sender_type, a.visible, a.from_customer)
        name, address = split_sender(a.sender)
        body = a.body or ""
        if first_message:
            # The opening message keeps its forwards: a cover note saying "see
            # below" above the real problem is common.
            source, first_message = body, False
        else:
            source = split_body(body).clean or body
        text = _drop_own_name(clean_text(source, boilerplate_for(address), sender=address),
                              name, address)
        if not text.strip() and kind == "customer":
            # A customer's one-word reply ("Approved") can be the message.
            text = _drop_own_name(clean_text(source, Boilerplate(), sender=address),
                                  name, address)
        if not text.strip():
            tl.empty_count += 1
            continue
        if len(text) > TEXT_CAP:
            text = text[:TEXT_CAP] + " […]"
        items.append((a.created or _EARLIEST, 1, a.number, {
            "no": a.number, "at": _when(a.created), "kind": kind, "from": name or address,
            "subject": (a.subject or "").strip(), "text": text}))

    tl.entries = [e for *_, e in sorted(items, key=lambda x: x[:3])]
    tl.queue_path = path(tl.queue_path)
    if not tl.queue_path and ticket.queue:
        tl.queue_path = [ticket.queue]        # never moved: it closed where it started
    return tl


def render(ticket: dict, entries: list[dict], queue_path: list[str],
           names: bool = True) -> str:
    """The timeline as plain text, one block per entry.

    names=False leaves out who wrote each message: a model told not to name
    people still copies a name it can see. The role is what it needs.
    """
    head = [
        f"Ticket {ticket.get('ticket_number')}: {ticket.get('title') or ''}".strip(),
        f"Product: {ticket.get('product') or '-'}   Closed in queue: {ticket.get('queue') or '-'}"
        f"   Type: {ticket.get('type') or '-'}   Subtype: {ticket.get('subtype') or '-'}",
        f"Queue path recorded by the ticket system: "
        f"{' -> '.join(queue_path) if queue_path else 'none recorded'}",
        "",
    ]
    labels = {"customer": "CUSTOMER", "reply": "REPLY TO CUSTOMER",
              "note": "INTERNAL NOTE", "queue": "MOVED TO QUEUE"}
    body: list[str] = []
    for e in entries:
        if e["kind"] == "queue":
            line = f"[move] {e.get('at') or ''}  MOVED TO QUEUE: {e.get('queue')}"
            if e.get("from_queue"):
                line += f" (from {e['from_queue']})"
            body.append(line)
            if e.get("reason"):
                body.append(f"Reason given: {e['reason']}")
            if e.get("team"):
                body.append(f"Responsible team: {e['team']}")
            body.append("")
            continue
        who = f" ({e['from']})" if names and e.get("from") else ""
        body.append(f"[{e.get('no')}] {e.get('at') or ''}  {labels[e['kind']]}{who}")
        if e.get("subject"):
            body.append(f"Subject: {e['subject']}")
        body.append(e.get("text") or "(no text)")
        body.append("")
    return "\n".join(head + body).strip() + "\n"
