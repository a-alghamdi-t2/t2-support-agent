"""The audit log: what a person did.

Only decisions and changes made by people are written here -- approving,
editing or rejecting a draft, uploading a file, and adding,
removing or changing a value. One row per thing done, naming who did it.
What the agent does on its own is not audit: its reasoning and steps are kept
with the draft (drafts.trace), its checks in their own tables, and the result
of each write in ticket_writes.
"""

from __future__ import annotations

from typing import Any

from support_agent.db import postgres
from support_agent.logging import get_logger

log = get_logger(__name__)

ACTIONS = ("approve", "edit", "reject", "upload", "add", "remove", "change")

MAX_VALUE = 2000
"""Longer values (a pasted document, a long guidance text) are cut to this."""


def _text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool):
        text = "on" if value else "off"
    elif isinstance(value, (list, tuple, set)):
        text = ", ".join(str(v) for v in value)
    else:
        text = str(value)
    return text if len(text) <= MAX_VALUE else text[:MAX_VALUE] + " ..."


def record(
    *,
    who: str,
    action: str,
    what: str,
    name: str | None = None,
    old: Any = None,
    new: Any = None,
    ticket_id: int | None = None,
    draft_id: int | None = None,
) -> None:
    """Write one row. Never raises: a failure here must not undo what the
    person just did.

    who:   the person's email.
    what:  what was acted on: "draft", "setting", "queue", "urgent keyword" ...
    name:  which one: the setting key, the value, the file name ...
    old / new: before and after, for a change. Never pass a secret's value.
    """
    if action not in ACTIONS:
        log.error("audit action not allowed", extra={"action": action, "what": what})
        return
    try:
        postgres.execute(
            """
            INSERT INTO audit_log (who, action, what, name, old_value, new_value,
                                   ticket_id, draft_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            ((who or "unknown").strip().lower(), action, what, _text(name),
             _text(old), _text(new), ticket_id, draft_id),
        )
    except Exception:
        log.exception("audit write failed", extra={"action": action, "what": what})
