"""The signature added under every reply sent to a customer.

Set on the AI admin page, per product, with one for every product used when a
product has none. Each has an Arabic and an English text: a reply written in
Arabic gets the Arabic one, any other the English one, and when the one
wanted is empty the other is used.

The agent writes replies without a sign-off (prompts/orchestrator/route.md):
the signature is added here, when the reply is approved, so it is the same on
every reply and changes in one place.
"""

from __future__ import annotations

import re

from support_agent.db import audit, postgres

ARABIC = re.compile(r"[؀-ۿݐ-ݿࢠ-ࣿﭐ-﷿ﹰ-﻿]")
MAX_LENGTH = 1000


class SignatureError(ValueError):
    pass


def _row(code: str | None) -> dict | None:
    return postgres.query_one(
        "SELECT * FROM reply_signatures WHERE coalesce(upper(product), '') = %s",
        ((code or "").strip().upper(),))


def get(code: str | None) -> dict | None:
    """The signature set for a product (code), or for every product (None).
    Only that one: no falling back. For the admin page."""
    row = _row(code)
    return {"product": row["product"], "arabic": row["arabic"], "english": row["english"],
            "updated_by": row["updated_by"], "updated_at": row["updated_at"]} if row else None


def is_arabic(text: str) -> bool:
    """Mostly Arabic letters, among the letters it has."""
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return False
    return sum(1 for c in letters if ARABIC.match(c)) * 2 >= len(letters)


def for_reply(code: str, reply: str) -> str | None:
    """The signature for a reply in this product: the product's own, else the
    one for every product; in the reply's language, else the other one."""
    for row in (_row(code), _row(None)):
        if not row or not (row["arabic"] or row["english"]):
            continue
        first, second = (("arabic", "english") if is_arabic(reply) else ("english", "arabic"))
        return (row[first] or row[second]).strip()
    return None


def sign(code: str, reply: str) -> str:
    """The reply with its signature under it."""
    signature = for_reply(code, reply)
    return reply.rstrip() + ("\n\n" + signature if signature else "")


def save(code: str | None, *, arabic: str | None, english: str | None, who: str) -> None:
    """Set a product's signature (code), or the one for every product (None).
    Both empty removes it."""
    arabic = (arabic or "").strip() or None
    english = (english or "").strip() or None
    for text in (arabic, english):
        if text and len(text) > MAX_LENGTH:
            raise SignatureError(f"at most {MAX_LENGTH} characters")
    code = (code or "").strip().upper() or None
    before = _row(code)
    if before and (before["arabic"], before["english"]) == (arabic, english):
        return
    if not before and not (arabic or english):
        return
    if not (arabic or english):
        postgres.execute("DELETE FROM reply_signatures WHERE id = %s", (before["id"],))
    elif before:
        postgres.execute("UPDATE reply_signatures SET arabic = %s, english = %s, "
                         "updated_by = %s WHERE id = %s", (arabic, english, who, before["id"]))
    else:
        postgres.execute("INSERT INTO reply_signatures (product, arabic, english, updated_by) "
                         "VALUES (%s, %s, %s, %s)", (code, arabic, english, who))

    def shown(row_ar, row_en):
        parts = [p for p in (row_ar, row_en) if p]
        return "\n---\n".join(parts) or None

    audit.record(who=who, action="change", what="signature", name=code or "all products",
                 old=shown(before["arabic"], before["english"]) if before else None,
                 new=shown(arabic, english))
