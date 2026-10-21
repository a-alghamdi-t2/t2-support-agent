"""What a reviewer does to a draft: approve, edit, reject, draft again.

Called by our API (component 10) on behalf of a staff member of the ticket
system, named by their email. Nothing here talks to the ticket system except
one check: before Approve & Send to a sender our copy says is pending, the
ticket is read again, in case the contact was approved on their page since.

Approving records the decision and queues the writes in ticket_writes; the
writer (review/writer.py) sends them. A reviewer's click never waits on their
system being up, and a write that fails is retried without asking again.

    approve(send=True)    Approve & Send: the routing with an internal note,
                          then the reply, which their system emails
    approve(send=False)   Approve & Draft (Approve when there is no reply):
                          the routing with an internal note holding the
                          reply for staff to send themselves
    approve(changes=...)  Edit: the reviewer's values, checked, then as above
    reject(reason)        nothing is written
    redraft(product)      Draft again for this product: the agent drafts the
                          ticket again for that product; the old draft is
                          recorded as rejected, with the product change as
                          the reason, and the new one points at it
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from support_agent import options
from support_agent.config import get_settings
from support_agent.db import audit, postgres
from support_agent.logging import get_logger
from support_agent.review import note as ticket_note
from support_agent.review import signatures

log = get_logger(__name__)

EDITABLE = ("action", "product", "queue", "type", "subtype", "conclusions", "next_state",
            "priority", "reply_body")
"""What a reviewer can change."""

KEPT = ("action", "reply_kind", "reply_body", "product", "queue", "queue_reason",
        "next_state", "type", "subtype", "conclusions", "priority", "issue_type",
        "summary", "collected", "missing", "note", "confidence", "reasoning")
"""The draft's fields, as approved, kept in reviews.final."""


class ReviewError(ValueError):
    """The action cannot be done as asked. The message is for the reviewer."""


class NotPending(ReviewError):
    """Someone got there first, or the draft was replaced."""


@dataclass
class Approval:
    draft_id: int
    review_id: int
    outcome: str
    writes: list[str] = field(default_factory=list)
    """"routing", and "reply" for Approve & Send. Empty for spam."""
    changed_fields: list[str] = field(default_factory=list)


def _load(draft_id: int) -> dict:
    row = postgres.query_one(
        """
        SELECT d.*, t.ticket_number, t.title, t.t2_ticket_id, t.customer_contact_status
        FROM drafts d JOIN tickets t ON t.id = d.ticket_id WHERE d.id = %s
        """, (draft_id,))
    if row is None:
        raise ReviewError(f"no draft {draft_id}")
    if row["status"] != "pending":
        raise NotPending(f"this draft is {row['status']}, not waiting for review")
    return row


def _who(who: str) -> str:
    who = (who or "").strip().lower()
    if "@" not in who[1:]:
        raise ReviewError("the reviewer must be named by their email")
    return who


# ---------------------------------------------------------------------------
# checking the reviewer's values
# ---------------------------------------------------------------------------

def _reason_of(code: str | None, type_name: str | None, subtype: str) -> str | None:
    o = options.find("subtype", subtype, product=code, parent=type_name) if code else None
    return o.conclusion if o else None


def _check(final: dict, changed: set[str], agent: dict) -> None:
    """Every value the reviewer changed must be one the agent may choose now,
    and everything that depends on a changed product or type must belong to
    it. The agent's own values are left alone: they were valid when drafted."""
    row_product, row_type = agent["product"], agent["type"]
    code = final["product"]
    product_changed = "product" in changed
    if product_changed:
        o = options.product(code or "")
        if o is None or o.removed or (o.product or "").upper() == \
                get_settings().intake_product.upper():
            raise ReviewError(f"{code!r} is not a product")
        final["product"] = code = o.product

    if final.get("queue") and (product_changed or "queue" in changed):
        q = options.find("queue", final["queue"], product=code)
        if q is None:
            raise ReviewError(f"{final['queue']!r} is not a queue of {code}")
        final["queue"] = q.value
    elif product_changed and not final.get("queue") and options.queues(code):
        raise ReviewError(f"choose one of {code}'s queues")

    type_changed = product_changed or "type" in changed
    if final.get("type") and type_changed:
        t = options.find("type", final["type"], product=code)
        if t is None:
            raise ReviewError(f"{final['type']!r} is not a type of {code}")
        final["type"] = t.value

    if type_changed and "subtype" not in changed and final.get("subtype"):
        # The agent's subtype belonged to the type it chose. Under another
        # type it is cleared, and so is Conclusions when it was its reason.
        if final.get("conclusions") and final["conclusions"] == _reason_of(
                row_product, row_type, final["subtype"]):
            final["conclusions"] = None
        final["subtype"] = None

    if final.get("subtype") and (type_changed or "subtype" in changed):
        s = options.find("subtype", final["subtype"], product=code, parent=final.get("type"))
        if s is None:
            raise ReviewError(f"{final['subtype']!r} is not a subtype of {final.get('type')}")
        final["subtype"] = s.value
        # Their screen expects the catalog's reason for the subtype.
        if s.conclusion:
            final["conclusions"] = s.conclusion

    for kind, key in (("next_state", "next_state"), ("priority", "priority")):
        if final.get(key) and key in changed:
            o = options.find(kind, final[key])
            if o is None:
                raise ReviewError(f"{final[key]!r} is not a {'status' if kind == 'next_state' else kind}")
            final[key] = o.value

    if final.get("conclusions") and len(final["conclusions"]) > 500:
        raise ReviewError("Conclusions can be at most 500 characters")


def _contact_pending(row: dict, t2) -> bool:
    """Whether the ticket system still has the sender as a pending contact.
    Our copy may be behind: someone may have approved the contact on their
    page since. Asked again only when our copy says pending."""
    if (row["customer_contact_status"] or "").lower() != "pending":
        return False
    own = t2 is None
    try:
        if own:
            from support_agent.ticketsystem import connect
            t2 = connect()
        ticket = t2.get(row["t2_ticket_id"], articles=False)
    except Exception as exc:          # could not ask: the writer will hold it if needed
        log.warning("contact not checked", extra={"ticket": row["ticket_number"],
                                                  "error": str(exc)[:200]})
        return False
    finally:
        if own and t2 is not None:
            t2.close()
    postgres.execute("UPDATE tickets SET customer_contact_status = %s, customer_org = %s, "
                     "customer_id = %s, last_synced_at = now() WHERE id = %s",
                     (ticket.customer_status or None, ticket.customer_org or None,
                      ticket.customer_id or None, row["ticket_id"]))
    return ticket.customer_status.lower() == "pending"


# ---------------------------------------------------------------------------
# the actions
# ---------------------------------------------------------------------------

def approve(draft_id: int, *, who: str, send: bool = False, changes: dict | None = None,
            comment: str | None = None, t2=None) -> Approval:
    """Approve a draft, as it is or with the reviewer's changes, and queue
    what is written to the ticket. See the module docstring."""
    who = _who(who)
    row = _load(draft_id)
    changes = {k: v for k, v in (changes or {}).items()}
    unknown = set(changes) - set(EDITABLE)
    if unknown:
        raise ReviewError(f"cannot change {', '.join(sorted(unknown))}")

    final = {k: row[k] for k in KEPT}
    for k, v in changes.items():
        final[k] = (v.strip() or None) if isinstance(v, str) else v
    changed = {k for k in changes if final[k] != row[k]}
    if final["action"] not in ("answer", "route", "spam"):
        raise ReviewError("the action is answer, route or spam")
    if final["action"] == "spam" and row["action"] != "spam":
        raise ReviewError("only the agent proposes spam; use Mark as Spam on the ticket page")

    # Spam, approved as spam: their Mark as Spam does the work, nothing is
    # written by us.
    if final["action"] == "spam":
        if send:
            raise ReviewError("a spam proposal has no reply to send")
        return _record(row, final, who, outcome="approve", changed=[], diff={},
                       comment=comment, send=False, writes=[])

    if not final.get("product"):
        raise ReviewError("choose the product")
    _check(final, changed, row)

    reply = (final.get("reply_body") or "").strip()
    if final["action"] == "route":
        if send:
            raise ReviewError("a routing has no reply to send: change it to an answer first")
        final["reply_body"] = final["reply_kind"] = None
        reply = ""
    elif not reply:
        raise ReviewError("an answer needs a reply")
    elif not final.get("reply_kind"):
        final["reply_kind"] = "answer"

    if send and _contact_pending(row, t2):
        raise ReviewError("the sender is a pending contact: approve or link the contact on "
                          "the ticket page first, or use Approve & Draft")

    diff = {k: {"from": row[k], "to": final[k]} for k in EDITABLE if k in changed}
    for k in ("subtype", "conclusions"):          # what _check filled in, after a change
        if k not in diff and final[k] != row[k]:
            diff[k] = {"from": row[k], "to": final[k]}

    product = options.product(final["product"])
    signed = signatures.sign(final["product"], reply) if reply else None
    reply_state = "sent" if send else ("to_send" if reply else "none")
    urgent = postgres.query_one("SELECT agent_reason FROM draft_urgent WHERE draft_id = %s",
                                (draft_id,))
    body = ticket_note.render(final=final, product_name=product.value if product else final["product"],
                              reviewer=who, reply=reply_state, changes=diff,
                              urgent_reason=urgent["agent_reason"] if urgent else None,
                              comment=comment, reply_text=signed)
    writes = [("routing", {
        "product": final["product"], "queue": final.get("queue"), "type": final.get("type"),
        "subtype": final.get("subtype"), "conclusions": final.get("conclusions"),
        "next_state": final.get("next_state"), "priority": final.get("priority"),
        "note": {"subject": ticket_note.subject(final, reply=reply_state), "body": body}})]
    if send:
        title = (row["title"] or "").strip() or "Your request"
        writes.append(("reply", {"subject": title if title.lower().startswith("re:")
                                 else f"Re: {title}", "body": signed}))
    return _record(row, final, who, outcome="edit" if diff else "approve",
                   changed=sorted(diff), diff=diff, comment=comment, send=send, writes=writes)


def _record(row: dict, final: dict, who: str, *, outcome: str, changed: list[str], diff: dict,
            comment: str | None, send: bool, writes: list[tuple[str, dict]]) -> Approval:
    with postgres.connection() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE drafts SET status = 'approved' WHERE id = %s "
                        "AND status = 'pending'", (row["id"],))
            if cur.rowcount == 0:
                raise NotPending("this draft was reviewed or replaced a moment ago")
            cur.execute(
                """
                INSERT INTO reviews (draft_id, reviewed_by, outcome, send_reply, final,
                                     changed_fields, changes, comment)
                VALUES (%s, %s, %s, %s, %s::jsonb, %s, %s::jsonb, %s) RETURNING id
                """,
                (row["id"], who, outcome, send,
                 json.dumps(final, ensure_ascii=False, default=str), changed,
                 json.dumps(diff, ensure_ascii=False, default=str),
                 (comment or "").strip() or None))
            review_id = cur.fetchone()["id"]
            for kind, payload in writes:
                cur.execute("INSERT INTO ticket_writes (draft_id, kind, payload) "
                            "VALUES (%s, %s, %s::jsonb)",
                            (row["id"], kind, json.dumps(payload, ensure_ascii=False)))

    what = ("spam" if final["action"] == "spam" else
            f"{final['product']} / {final.get('queue') or '-'}"
            + (", reply to be sent" if send else ""))
    detail = "; ".join(f"{k}: {v['from'] or '-'} -> {v['to'] or '-'}"
                       for k, v in diff.items() if k != "reply_body")
    if "reply_body" in diff:
        detail = (detail + "; " if detail else "") + "reply edited"
    audit.record(who=who, action=outcome, what="draft", name=row["ticket_number"],
                 old=detail or None, new=what, ticket_id=row["ticket_id"], draft_id=row["id"])
    log.info("approved", extra={"draft": row["id"], "outcome": outcome, "send": send,
                                "changed": len(changed)})
    return Approval(row["id"], review_id, outcome, [k for k, _ in writes], changed)


def reject(draft_id: int, *, who: str, reason: str) -> int:
    """Reject a draft. Nothing is written to the ticket. Returns the review."""
    who = _who(who)
    reason = (reason or "").strip()
    if not reason:
        raise ReviewError("say why it is rejected")
    row = _load(draft_id)
    return _reject(row, who, reason, status_from="pending")


def _reject(row: dict, who: str, reason: str, *, status_from: str) -> int:
    with postgres.connection() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE drafts SET status = 'rejected' WHERE id = %s AND status = %s",
                        (row["id"], status_from))
            if cur.rowcount == 0:
                raise NotPending("this draft was reviewed or replaced a moment ago")
            cur.execute("INSERT INTO reviews (draft_id, reviewed_by, outcome, justification) "
                        "VALUES (%s, %s, 'reject', %s) RETURNING id", (row["id"], who, reason))
            review_id = cur.fetchone()["id"]
    audit.record(who=who, action="reject", what="draft", name=row["ticket_number"],
                 new=reason, ticket_id=row["ticket_id"], draft_id=row["id"])
    log.info("rejected", extra={"draft": row["id"]})
    return review_id


def redraft(draft_id: int, *, who: str, product: str) -> int:
    """Draft again for this product. Returns the new draft's id.

    The new draft is made first: if the agent fails, the old one is still
    waiting for review and nothing is lost. Making it supersedes the old one,
    which is then recorded as rejected by this reviewer, with the product
    change as the reason, so both stay for us to learn from."""
    who = _who(who)
    row = _load(draft_id)
    o = options.product(product or "")
    if o is None or o.removed or (o.product or "").upper() == \
            get_settings().intake_product.upper():
        raise ReviewError(f"{product!r} is not a product")
    if row["action"] != "spam" and (o.product or "") == (row["product"] or ""):
        raise ReviewError("that is already the draft's product")

    from support_agent.orchestrator import pipeline     # the agent; heavy to import

    new_id = pipeline.draft(row["ticket_id"], product=o.product, replaces=draft_id)
    _reject(row, who, f"wrong product: {row['product'] or 'spam'} -> {o.product}; "
                      "drafted again for that product", status_from="superseded")
    return new_id


# ---------------------------------------------------------------------------
# writes a person has to look at
# ---------------------------------------------------------------------------

def _write(write_id: int, status: str) -> dict:
    row = postgres.query_one(
        "SELECT w.*, d.ticket_id, t.ticket_number FROM ticket_writes w "
        "JOIN drafts d ON d.id = w.draft_id JOIN tickets t ON t.id = d.ticket_id "
        "WHERE w.id = %s", (write_id,))
    if row is None:
        raise ReviewError(f"no write {write_id}")
    if row["status"] != status:
        raise ReviewError(f"this write is {row['status']}, not {status}")
    return row


def send_again(write_id: int, *, who: str) -> None:
    """A held reply, once the reviewer fixed why (approved the contact, say):
    the writer tries it again."""
    who = _who(who)
    row = _write(write_id, "held")
    postgres.execute("UPDATE ticket_writes SET status = 'pending', last_error = NULL, "
                     "resolved_by = %s WHERE id = %s AND status = 'held'", (who, write_id))
    audit.record(who=who, action="change", what=f"{row['kind']} write",
                 name=row["ticket_number"], old="held", new="send again",
                 ticket_id=row["ticket_id"], draft_id=row["draft_id"])


def settle(write_id: int, *, who: str, happened: bool) -> None:
    """An uncertain write, once a person checked: happened=True when it is on
    the ticket (or the email is in the mailbox's Sent folder), False when it
    is not, and it is tried again."""
    who = _who(who)
    row = _write(write_id, "uncertain")
    postgres.execute(
        "UPDATE ticket_writes SET status = %s, resolved_by = %s, "
        "delivered_at = CASE WHEN %s THEN now() ELSE delivered_at END, "
        "last_error = CASE WHEN %s THEN last_error ELSE NULL END "
        "WHERE id = %s AND status = 'uncertain'",
        ("sent" if happened else "pending", who, happened, happened, write_id))
    audit.record(who=who, action="change", what=f"{row['kind']} write",
                 name=row["ticket_number"], old="uncertain",
                 new="it happened" if happened else "it did not happen: try again",
                 ticket_id=row["ticket_id"], draft_id=row["draft_id"])
