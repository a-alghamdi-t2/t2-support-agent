"""Team scopes: what each team handles, per product.

One short Markdown document per product, read whole by the agent before it
chooses a queue (orchestrator/route.py), plus one for every product, read
alongside. Not indexed and not split: its value is in the lines between
teams, which only make sense read together.

Every save is a new version; the newest is in use, and the older ones stay
for the audit trail.
"""

from __future__ import annotations

from support_agent import options
from support_agent.config import get_settings
from support_agent.db import audit, postgres

MAX_BYTES = 200 * 1024
TYPES = (".md", ".txt")


class ScopesError(ValueError):
    pass


def _code(code: str | None) -> str | None:
    if not code:
        return None
    o = options.product(code)
    if o is None or o.removed or (o.product or "").upper() == \
            get_settings().intake_product.upper():
        raise ScopesError(f"{code!r} is not a product")
    return o.product


def current(code: str | None) -> dict | None:
    """The version in use for a product (code), or for every product (None),
    with how many versions there have been."""
    return postgres.query_one(
        """
        SELECT id, product, content, filename, uploaded_by, created_at,
               (SELECT count(*) FROM team_scopes s2
                WHERE coalesce(upper(s2.product), '') = coalesce(upper(s.product), '')) AS versions
        FROM team_scopes s
        WHERE coalesce(upper(product), '') = %s
        ORDER BY id DESC LIMIT 1
        """, ((code or "").upper(),))


def save(code: str | None, raw: bytes | str, *, who: str, filename: str | None = None) -> dict:
    """A new version for a product (code) or every product (None)."""
    who = (who or "").strip().lower()
    if "@" not in who[1:]:
        raise ScopesError("say who is doing this: an email")
    code = _code(code)
    name = (filename or "").strip() or "team-scopes.md"
    if not name.lower().endswith(TYPES):
        raise ScopesError("team scopes are a Markdown (.md) or text (.txt) file")
    if isinstance(raw, bytes):
        if len(raw) > MAX_BYTES:
            raise ScopesError(f"larger than {MAX_BYTES // 1024} KB: this is a short document "
                              "the agent reads whole")
        try:
            raw = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            raise ScopesError("the file is not UTF-8 text") from None
    text = raw.replace("\r\n", "\n").strip()
    if not text:
        raise ScopesError("the file is empty")
    before = current(code)
    if before and before["content"] == text:
        return before
    row = postgres.query_one(
        "INSERT INTO team_scopes (product, content, filename, uploaded_by) "
        "VALUES (%s, %s, %s, %s) RETURNING *", (code, text, name, who))
    audit.record(who=who, action="upload", what="team scopes", name=code or "all products",
                 old=f"version {before['versions']}" if before else None,
                 new=f"version {(before['versions'] if before else 0) + 1}: {name}")
    return row
