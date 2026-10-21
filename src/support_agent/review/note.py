"""The internal note written on the ticket when a draft is approved.

Staff see it on the ticket page; the customer never does. It says what the
agent proposed and why, who approved it, what they changed, and -- for
Approve & Draft -- the reply for staff to check and send themselves.

Plain text, English labels; the customer's language only inside the reply.
"""

from __future__ import annotations

from support_agent.config import get_settings

RULE = "-" * 60

LABELS = {"product": "Product", "queue": "Queue", "type": "Type", "subtype": "Subtype",
          "conclusions": "Conclusions", "next_state": "Status", "priority": "Priority",
          "action": "Action", "reply_body": "Reply"}


def subject(final: dict, *, reply: str) -> str:
    """reply: "sent", "to_send" or "none"."""
    agent = get_settings().agent_name
    where = f"routed to {final['queue']}" if final.get("queue") else "classified"
    tail = {"sent": ", reply sent", "to_send": ", reply to check and send"}.get(reply, "")
    return f"{agent}: {where}{tail}"


def render(*, final: dict, product_name: str, reviewer: str, reply: str,
           changes: dict, urgent_reason: str | None = None, comment: str | None = None,
           reply_text: str | None = None) -> str:
    """The note body.

    final        the fields as approved
    reply        "sent": emailed as its own message; "to_send": written here
                 for staff (Approve & Draft); "none"
    changes      {field: {"from": agent's, "to": reviewer's}}
    reply_text   the reply as it would be sent, signature included
    """
    said = {"sent": "the reply was emailed to the customer as its own message",
            "to_send": "the reply below was NOT sent: check it and send it from this page",
            "none": "no reply to the customer"}[reply]
    lines = [f"Approved by {reviewer}: {said}.", RULE]

    if urgent_reason:
        lines += ["URGENT", urgent_reason.strip(), ""]

    lines += ["REQUEST", (final.get("summary") or "").strip(), ""]

    lines.append("CLASSIFICATION")
    for key, value in (("product", product_name), ("queue", final.get("queue")),
                       ("type", final.get("type")), ("subtype", final.get("subtype")),
                       ("conclusions", final.get("conclusions")),
                       ("next_state", final.get("next_state")),
                       ("priority", final.get("priority"))):
        label = LABELS[key] + ":"
        lines.append(f"  {label:<13}{value or '-'}")
    lines.append("")

    why = [x for x in ((final.get("reasoning") or "").strip(),
                       f"Queue: {final['queue_reason'].strip()}"
                       if final.get("queue_reason") else "") if x]
    if why:
        lines += ["WHY", *why, ""]

    if changes:
        lines.append("CHANGED BY THE REVIEWER")
        for field, c in changes.items():
            if field == "reply_body":
                lines.append("  Reply: edited")
                continue
            lines.append(f"  {LABELS.get(field, field)}: {c.get('from') or '-'} -> "
                         f"{c.get('to') or '-'}")
        lines.append("")

    if comment and comment.strip():
        lines += ["REVIEWER'S COMMENT", comment.strip(), ""]

    missing = [m for m in (final.get("missing") or []) if str(m).strip()]
    if missing:
        lines += ["STILL TO FIND OUT", *[f"  - {m}" for m in missing], ""]

    if reply == "to_send" and reply_text:
        lines += ["REPLY TO SEND", RULE, reply_text.strip(), RULE]
    else:
        lines.append(RULE)
    return "\n".join(lines)
