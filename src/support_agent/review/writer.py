"""Writing approved drafts to the ticket.

Runs every WRITER_SECONDS (runtime.py), and right after an approval. Each
approved draft has a routing write, and for Approve & Send a reply write; an
approved spam proposal has a spam write only:

    routing   product, queue, type, subtype, Conclusions, status, priority and
              the internal note, in one TicketUpdate
    reply     the reply on its own, which their system emails to the
              customer. Only after the routing is written: the ticket must be
              classified before it can be replied to, and an email that fails
              then undoes nothing but itself.
    spam      ContactMarkSpam: the sender, still a pending contact, blocked
              and their open tickets closed. Contact-wide, so never repeated
              blindly: after no answer, the ticket is read to see whether the
              contact is blocked.

Each write ends in one of these (write_status):

    sent       done
    pending    not done yet, tried again on the next pass: their system was
               down or could not be reached
    held       refused before anything was sent (the contact is pending, the
               ticket is not classified, notifications are off, another check
               before sending): a person fixes it and sends it again
    uncertain  it may have happened: no answer came back, or the email was
               accepted and not saved. Never repeated by itself: a person
               checks the ticket, or the mailbox's Sent folder, and settles it
    failed     not written, for good: someone else handled the ticket since
               the draft was made, or a value no longer exists

Just before a routing write the ticket is read again: if a person gave it a
product, a queue or a classification, or closed it, since the draft was made,
nothing is written and their change stands.

With DRY_RUN on, nothing is written: the writes wait.
"""

from __future__ import annotations

from support_agent.config import get_settings
from support_agent.db import postgres
from support_agent.intake.poller import left_intake
from support_agent.logging import get_logger
from support_agent.ticketsystem import Note, T2Error

log = get_logger(__name__)

MAX_ATTEMPTS = 10
"""A write their system keeps failing to answer is given up after this many
passes, and shows as failed."""


def _due(limit: int) -> list[dict]:
    return postgres.query(
        """
        SELECT w.*, t.id AS ticket_id, t.t2_ticket_id, t.ticket_number
        FROM ticket_writes w
        JOIN drafts d ON d.id = w.draft_id
        JOIN tickets t ON t.id = d.ticket_id
        WHERE w.status = 'pending'
          AND (w.kind IN ('routing', 'spam') OR EXISTS (
                SELECT 1 FROM ticket_writes r
                WHERE r.draft_id = w.draft_id AND r.kind = 'routing' AND r.status = 'sent'))
        ORDER BY w.created_at, w.id
        LIMIT %s
        """, (limit,))


def _set(write_id: int, status: str, *, error: str | None = None, article: str | None = None,
         message: str | None = None, attempt: bool = True) -> None:
    postgres.execute(
        """
        UPDATE ticket_writes SET status = %s, last_error = %s,
               attempts = attempts + %s,
               t2_article_id = coalesce(%s, t2_article_id),
               message_id = coalesce(%s, message_id),
               delivered_at = CASE WHEN %s = 'sent' THEN now() ELSE delivered_at END
        WHERE id = %s
        """,
        (status, (error or None) and error[:1000], 1 if attempt else 0, article, message,
         status, write_id))


def _give_up_reply(draft_id: int, why: str) -> None:
    """The routing was not written, so neither is the reply."""
    postgres.execute("UPDATE ticket_writes SET status = 'failed', last_error = %s "
                     "WHERE draft_id = %s AND kind = 'reply' AND status = 'pending'",
                     (f"not sent: {why}", draft_id))


def _routing(t2, w: dict) -> str:
    p = w["payload"]
    try:
        ticket = t2.get(w["t2_ticket_id"], articles=False)
    except T2Error as exc:
        if exc.refused:
            _set(w["id"], "failed", error=f"the ticket could not be read: {exc}")
            _give_up_reply(w["draft_id"], "the ticket could not be read")
            return "failed"
        return _retry(w, exc)
    why = left_intake(ticket, t2.intake_product)
    if why:
        why = f"not written: the ticket was {why} after the draft was made"
        _set(w["id"], "failed", error=why)
        _give_up_reply(w["draft_id"], "the routing was not written")
        log.warning("routing not written", extra={"ticket": w["ticket_number"], "why": why})
        return "failed"

    try:
        r = t2.update(w["t2_ticket_id"], product=p["product"], queue=p.get("queue"),
                      state=p.get("next_state"), type=p.get("type"),
                      priority=p.get("priority"), subtype=p.get("subtype"),
                      conclusions=p.get("conclusions"),
                      note=Note(p["note"]["subject"], p["note"]["body"],
                                sender=get_settings().agent_name))
    except T2Error as exc:
        if exc.maybe_applied:
            _set(w["id"], "uncertain", error=f"no answer came back: check the ticket ({exc})")
            return "uncertain"
        if exc.refused:
            _set(w["id"], "failed", error=str(exc))
            _give_up_reply(w["draft_id"], "the routing was refused")
            return "failed"
        return _retry(w, exc)

    _set(w["id"], "sent", article=r.article_id)
    postgres.execute(
        "UPDATE tickets SET product_code = %s, queue = %s, state = coalesce(%s, state), "
        "type = coalesce(%s, type), subtype = %s, conclusions = %s, "
        "priority = coalesce(%s, priority), requires_classification = false, "
        "last_synced_at = now() WHERE id = %s",
        (p["product"], p.get("queue"), p.get("next_state"), p.get("type"), p.get("subtype"),
         p.get("conclusions"), p.get("priority"), w["ticket_id"]))
    log.info("routing written", extra={"ticket": w["ticket_number"], "note": r.article_id})
    return "sent"


def _reply(t2, w: dict) -> str:
    p = w["payload"]
    try:
        r = t2.reply(w["t2_ticket_id"], subject=p["subject"], body=p["body"])
    except T2Error as exc:
        if exc.email_not_tried:
            hint = {"contact pending": "approve or link the contact on the ticket page, "
                                       "then send it again",
                    "not classified": "classify the ticket, then send it again",
                    "notifications off": "the customer's email notifications are off",
                    "not attempted": "check the customer's email address and their "
                                     "email setup, then send it again",
                    }.get(exc.email_not_tried, "")
            _set(w["id"], "held", error=f"not sent: {exc.email_not_tried}; {hint}")
            return "held"
        if exc.email_uncertain:
            _set(w["id"], "uncertain",
                 error=f"the email may have gone out: check the mailbox's Sent folder "
                       f"before sending again ({exc})")
            return "uncertain"
        if exc.refused:
            _set(w["id"], "failed", error=str(exc))
            return "failed"
        return _retry(w, exc)            # never reached them: nothing was sent
    _set(w["id"], "sent", article=r.article_id, message=r.message_id)
    log.info("reply sent", extra={"ticket": w["ticket_number"], "message": r.message_id})
    return "sent"


SPAM_NOT_DONE = {
    "ContactMarkSpam.ContactNotPending": "the sender is no longer a pending contact "
                                         "(approved, linked or blocked since)",
    "ContactMarkSpam.ProtectedTicket": "the sender has an Availo test ticket",
    "ContactMarkSpam.NoClosedStatus": "their system has no closed status",
}


def _spam(t2, w: dict) -> str:
    try:
        r = t2.mark_spam(w["t2_ticket_id"])
    except T2Error as exc:
        if exc.maybe_applied:
            return _spam_unknown(t2, w, exc)
        if exc.refused:
            why = SPAM_NOT_DONE.get(exc.code, str(exc))
            _set(w["id"], "failed", error=f"not marked as spam: {why}")
            log.warning("spam not marked", extra={"ticket": w["ticket_number"], "why": why})
            return "failed"
        return _retry(w, exc)
    _spam_done(w, r.tickets_closed)
    return "sent"


def _spam_done(w: dict, closed: int | None) -> None:
    _set(w["id"], "sent")
    postgres.execute("UPDATE ticket_writes SET payload = payload || %s::jsonb WHERE id = %s",
                     (f'{{"tickets_closed": {closed if closed is not None else "null"}}}',
                      w["id"]))
    postgres.execute("UPDATE tickets SET customer_contact_status = 'Blocked', "
                     "last_synced_at = now() WHERE id = %s", (w["ticket_id"],))
    log.info("marked as spam", extra={"ticket": w["ticket_number"], "closed": closed})


def _spam_unknown(t2, w: dict, exc: T2Error) -> str:
    """No answer came back: the sender's status on the ticket says whether it
    happened. Blocked, it did; still pending, it did not, and it is tried
    again; anything else, or no answer again, a person looks."""
    try:
        status = t2.get(w["t2_ticket_id"], articles=False).customer_status.lower()
    except T2Error:
        status = ""
    if status == "blocked":
        _spam_done(w, None)
        return "sent"
    if status == "pending":
        return _retry(w, exc)
    _set(w["id"], "uncertain", error=f"no answer came back, and the sender is "
                                     f"{status or 'unknown'}: check the ticket ({exc})")
    return "uncertain"


def _retry(w: dict, exc: Exception) -> str:
    if w["attempts"] + 1 >= MAX_ATTEMPTS:
        _set(w["id"], "failed", error=f"gave up after {MAX_ATTEMPTS} tries: {exc}")
        if w["kind"] == "routing":
            _give_up_reply(w["draft_id"], "the routing was not written")
        return "failed"
    _set(w["id"], "pending", error=str(exc))
    return "pending"


def send_pending(*, limit: int = 20, t2=None) -> tuple[int, int]:
    """One pass: the routings and spam first, then the replies whose routing
    is written (including any written a moment ago, in this pass). Returns
    (written, not written). Never raises for one write."""
    if not _due(limit):
        return 0, 0
    if get_settings().dry_run:
        log.warning("dry run: nothing written to the ticket system",
                    extra={"waiting": len(_due(limit))})
        return 0, 0

    own = t2 is None
    if own:
        from support_agent.ticketsystem import connect
        t2 = connect()
    sent = other = 0
    try:
        for kind, write in (("routing", _routing), ("spam", _spam), ("reply", _reply)):
            for w in _due(limit):
                if w["kind"] != kind:
                    continue
                try:
                    outcome = write(t2, w)
                except Exception as exc:          # one bad write must not stop the rest
                    log.exception("write failed", extra={"write": w["id"]})
                    outcome = _retry(w, exc)
                if outcome == "sent":
                    sent += 1
                else:
                    other += 1
    finally:
        if own:
            t2.close()
    return sent, other
