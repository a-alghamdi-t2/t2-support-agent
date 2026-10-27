"""The internal note written on the ticket when a draft is approved.

Staff see it on the ticket page; the customer never does. It says who
approved it, what the customer asked, the classification, why the agent chose
the queue and the rest, what the reviewer changed, and the suggested reply:
sent, to check and send (Approve & Draft), or kept for the team on a routing.

Plain text, one section after another with a rule between them; English
labels, the customer's language only inside the reply.
"""

from __future__ import annotations

from support_agent.config import get_settings

RULE = "=" * 60

LABELS = {"product": "Product", "queue": "Queue", "type": "Type", "subtype": "Subtype",
          "conclusions": "Conclusions", "next_state": "Status", "priority": "Priority",
          "action": "Action", "reply_body": "Reply"}

REPLY_HEADINGS = {
    "sent": ("SUGGESTED REPLY", "Sent to the customer as its own message."),
    "to_send": ("SUGGESTED REPLY", "Not sent: check it and send it from this page."),
    "kept": ("SUGGESTED REPLY", "Not sent: the ticket was routed. Kept here in case it helps."),
}


def subject(final: dict, *, reply: str) -> str:
    """reply: "sent", "to_send", "kept" or "none"."""
    agent = get_settings().agent_name
    where = f"routed to {final['queue']}" if final.get("queue") else "classified"
    tail = {"sent": ", reply sent", "to_send": ", reply to check and send"}.get(reply, "")
    return f"{agent}: {where}{tail}"


def render(*, final: dict, product_name: str, reviewer: str, reply: str,
           changes: dict, urgent_reason: str | None = None, comment: str | None = None,
           reply_text: str | None = None) -> str:
    """The note body.

    final        the fields as approved
    reply        "sent": emailed as its own message; "to_send": for staff to
                 send (Approve & Draft); "kept": the agent's reply on a draft
                 approved as a routing; "none"
    changes      {field: {"from": agent's, "to": reviewer's}}
    reply_text   the reply as sent or to send, signature included
    """
    said = {"sent": "The reply was emailed to the customer.",
            "to_send": "The reply below was NOT sent.",
            "kept": "Routed; nothing was sent to the customer.",
            "none": "No reply to the customer."}[reply]
    sections: list[list[str]] = []

    if urgent_reason:
        sections.append(["URGENT", urgent_reason.strip()])

    sections.append(["REQUEST", (final.get("summary") or "").strip()])

    rows = ["CLASSIFICATION"]
    for key, value in (("product", product_name), ("queue", final.get("queue")),
                       ("type", final.get("type")), ("subtype", final.get("subtype")),
                       ("conclusions", final.get("conclusions")),
                       ("next_state", final.get("next_state")),
                       ("priority", final.get("priority"))):
        rows.append(f"  {LABELS[key] + ':':<13}{value or '-'}")
    sections.append(rows)

    if (final.get("queue_reason") or "").strip():
        sections.append(["QUEUE REASON", final["queue_reason"].strip()])
    if (final.get("reasoning") or "").strip():
        sections.append(["AGENT REASONING", final["reasoning"].strip()])

    if changes:
        rows = ["CHANGED BY THE REVIEWER"]
        for field, c in changes.items():
            if field == "reply_body":
                rows.append("  Reply: edited")
            else:
                rows.append(f"  {LABELS.get(field, field)}: {c.get('from') or '-'} -> "
                            f"{c.get('to') or '-'}")
        sections.append(rows)

    if comment and comment.strip():
        sections.append(["REVIEWER'S COMMENT", comment.strip()])

    missing = [str(m).strip() for m in (final.get("missing") or []) if str(m).strip()]
    if missing:
        sections.append(["STILL TO FIND OUT", *[f"  - {m}" for m in missing]])

    if reply in REPLY_HEADINGS and reply_text and reply_text.strip():
        heading, status = REPLY_HEADINGS[reply]
        sections.append([heading, status, "", reply_text.strip()])

    body = [f"Approved by {reviewer}. {said}"]
    for s in sections:
        body += ["", RULE, "", *s]
    return "\n".join(body)
