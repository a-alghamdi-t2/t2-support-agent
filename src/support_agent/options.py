"""The values the agent may choose: the ticket system's, with ours on top.

The products, queues, types, subtypes, statuses and priorities come from the
ticket system (TicketLookupsGet) and are copied into agent_options by sync().
Each row is the ticket system's value plus what only we add: a description of
when to use it (what the agent decides by), and whether it is hidden from the
agent. The reason that goes with a subtype is theirs too, refreshed on every
sync, and written to the ticket's Conclusions word for word.

sync() runs when the values are older than LOOKUPS_SECONDS (5 minutes) and the
agent is about to draft, and when the Values page opens. Each run:

  - adds what is new in the ticket system, marked New until someone writes its
    description or marks it seen
  - marks what is gone as removed: no longer offered, its description kept in
    case it comes back
  - brings back what returned
  - takes the subtype reasons and the statuses' open/closed as they are now

When the ticket system cannot be reached, nothing changes: the agent keeps the
values it has.

Rows by kind:
    product     product = its code, value = its name
    queue       product = code, value = queue
    type        product = code, value = type
    subtype     product = code, parent = its type, value = subtype,
                conclusion = its reason
    next_state  value = status, parent = "open" or "closed"
    priority    value = priority

There is always a default: one queue and one type per product, one status and
one priority for all. sync() chooses one wherever there is none (the first
sync, a new product, a default removed in the ticket system), and an AI admin
can make another value the default but not leave a kind without one. The
agent's choice falls back to it when what the model wrote is not a real value
(orchestrator.validate).
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

from support_agent.config import get_settings
from support_agent.db import audit, postgres
from support_agent.logging import get_logger

log = get_logger(__name__)

KINDS = ("product", "queue", "type", "subtype", "next_state", "priority")
DEFAULT_KINDS = ("queue", "type", "next_state", "priority")
"""Kinds that always have a default. Queues and types per product; statuses
and priorities for all."""
MAX_DESCRIPTION = 500
MAX_GUIDANCE = 2000

HIDE_ON_ARRIVAL_TYPES = ("default",)
"""Types never offered unless someone shows them: "Default" is what a ticket
gets when nobody chose a type."""


class OptionError(ValueError):
    pass


@dataclass(frozen=True)
class Option:
    id: int
    kind: str
    product: str | None
    parent: str | None
    value: str
    description: str | None
    conclusion: str | None
    hidden: bool
    is_new: bool
    removed: bool
    is_default: bool = False

    @property
    def offered(self) -> bool:
        return not self.hidden and not self.removed


@dataclass
class TypeChoice:
    """A type with the subtypes the agent may pick under it."""

    option: Option
    subtypes: list[Option] = field(default_factory=list)


@dataclass
class SyncResult:
    added: int = 0
    removed: int = 0
    returned: int = 0
    updated: int = 0
    defaults: int = 0
    """Defaults chosen because a kind had none."""
    skipped: str | None = None
    """Why nothing was read: "fresh" or the error."""


# ---------------------------------------------------------------------------
# sync
# ---------------------------------------------------------------------------

_last_sync = float("-inf")
_sync_lock = threading.Lock()


def _key(kind: str, product: str | None, parent: str | None, value: str) -> tuple:
    # A subtype is known by its type as well as its name; a status keeps its
    # row (and description) when it changes between open and closed.
    return (kind, (product or "").upper(),
            (parent or "").lower() if kind == "subtype" else "", value.strip().lower())


Wanted = tuple[str, str | None, str | None, str, str | None, int]
"""(kind, product, parent, value, conclusion, position)"""


def _wanted(lookups) -> list[Wanted]:
    rows: list[Wanted] = []
    for p in lookups.products:
        code = p.code.upper()
        rows.append(("product", code, None, p.name, None, 0))
        rows += [("queue", code, None, q, None, i) for i, q in enumerate(p.queues) if q]
        for t, (type_name, subtypes) in enumerate(p.types.items()):
            if not type_name:
                continue
            rows.append(("type", code, None, type_name, None, t))
            rows += [("subtype", code, type_name, s.name, s.reason, i)
                     for i, s in enumerate(subtypes) if s.name]
    rows += [("next_state", None, "closed" if closed else "open", name, None, i)
             for i, (name, closed) in enumerate(lookups.statuses) if name]
    rows += [("priority", None, None, name, None, i)
             for i, name in enumerate(lookups.priorities) if name]
    return rows


def _hidden_on_arrival(kind: str, product: str | None, value: str, *, first_sync: bool) -> bool:
    """Whether a value is kept from the agent when it first appears.

    - the intake product itself, and the type "Default": always.
    - a product added to the ticket system later: until an AI admin
      describes it. Choosing the product is the agent's first decision, and a
      bare new name next to described ones would draw tickets it should not.
      Its queues, types and subtypes are offered at once: they only come into
      play once the product is chosen. (On the very first sync nothing is
      hidden this way: every product is new then.)
    """
    if kind == "product":
        if (product or "").upper() == get_settings().intake_product.upper():
            return True
        return not first_sync
    return kind == "type" and value.strip().lower() in HIDE_ON_ARRIVAL_TYPES


def sync(*, force: bool = False, t2=None) -> SyncResult:
    """Bring agent_options in line with the ticket system. Never raises."""
    global _last_sync
    if not force and time.monotonic() - _last_sync < get_settings().lookups_seconds:
        return SyncResult(skipped="fresh")
    with _sync_lock:
        if not force and time.monotonic() - _last_sync < get_settings().lookups_seconds:
            return SyncResult(skipped="fresh")
        try:
            if t2 is None:
                from support_agent.ticketsystem import connect
                with connect() as client:
                    lookups = client.lookups()
            else:
                lookups = t2.lookups()
        except Exception as exc:          # down, unconfigured, refused: keep what we have
            log.warning("values not refreshed", extra={"error": str(exc)[:200]})
            return SyncResult(skipped=str(exc)[:200])
        result = _apply(_wanted(lookups))
        _last_sync = time.monotonic()
    invalidate()
    if result.added or result.removed or result.returned or result.updated:
        log.info("values refreshed", extra={"added": result.added, "removed": result.removed,
                                            "returned": result.returned,
                                            "updated": result.updated})
    return result


def _apply(wanted) -> SyncResult:
    result = SyncResult()
    with postgres.connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id, kind, product, parent, value, conclusion, position, "
                        "removed_at FROM agent_options")
            have = {_key(r["kind"], r["product"], r["parent"], r["value"]): r
                    for r in cur.fetchall()}
            first_sync = not have
            seen = set()
            for kind, product, parent, value, conclusion, position in wanted:
                key = _key(kind, product, parent, value)
                if key in seen:
                    continue
                seen.add(key)
                row = have.get(key)
                if row is None:
                    cur.execute(
                        """
                        INSERT INTO agent_options (kind, product, parent, value, conclusion,
                                                   hidden, position)
                        VALUES (%s, %s, %s, %s, %s, %s, %s)
                        """,
                        (kind, product, parent, value, conclusion,
                         _hidden_on_arrival(kind, product, value, first_sync=first_sync),
                         position))
                    result.added += 1
                    continue
                if row["removed_at"] is not None:
                    result.returned += 1
                changes = (row["value"] != value or (row["parent"] or None) != parent
                           or (kind == "subtype" and (row["conclusion"] or None)
                               != (conclusion or None)))
                if changes or row["removed_at"] is not None or row["position"] != position:
                    cur.execute(
                        """
                        UPDATE agent_options SET value = %s, parent = %s, position = %s,
                               conclusion = CASE WHEN kind = 'subtype' THEN %s ELSE conclusion END,
                               removed_at = NULL
                        WHERE id = %s
                        """,
                        (value, parent, position, conclusion, row["id"]))
                    if changes:
                        result.updated += 1
            for key, row in have.items():
                if key not in seen and row["removed_at"] is None:
                    cur.execute("UPDATE agent_options SET removed_at = now() WHERE id = %s",
                                (row["id"],))
                    result.removed += 1
            result.defaults = _ensure_defaults(cur)
    return result


def _pick_default(kind: str, rows: list[dict]) -> dict:
    """The value chosen as the default when a kind has none. A sensible
    start, which an AI admin can change: the Support queue, the first type
    in their order, Open, the lowest priority."""
    rows = sorted(rows, key=lambda r: (r["position"], r["value"].lower()))
    if kind == "queue":
        return next((r for r in rows if "support" in r["value"].lower()), rows[0])
    if kind == "next_state":
        return next((r for r in rows if r["value"].strip().lower() == "open"),
                    next((r for r in rows if r["parent"] == "open"), rows[0]))
    return rows[0]


def _ensure_defaults(cur) -> int:
    """A default wherever a kind has none offered. Not audited: nobody chose
    it, and the Values page shows it as chosen by the agent."""
    cur.execute("UPDATE agent_options SET is_default = false "
                "WHERE is_default AND (hidden OR removed_at IS NOT NULL)")
    cur.execute("SELECT id, kind, product, parent, value, position, is_default "
                "FROM agent_options WHERE kind = ANY(%s) AND NOT hidden "
                "AND removed_at IS NULL", (list(DEFAULT_KINDS),))
    groups: dict[tuple, list[dict]] = {}
    for r in cur.fetchall():
        groups.setdefault((r["kind"], r["product"] or ""), []).append(r)
    chosen = 0
    for (kind, _), rows in groups.items():
        if any(r["is_default"] for r in rows):
            continue
        cur.execute("UPDATE agent_options SET is_default = true WHERE id = %s",
                    (_pick_default(kind, rows)["id"],))
        chosen += 1
    return chosen


# ---------------------------------------------------------------------------
# reading
# ---------------------------------------------------------------------------

_cache: dict = {"rows": None}
_cache_lock = threading.Lock()


def invalidate() -> None:
    _cache["rows"] = None


def _waiting_for_description(r: dict) -> bool:
    """A product added later, hidden until someone describes it."""
    return (r["kind"] == "product" and r["hidden"] and r["description"] is None
            and r["updated_by"] is None and r["removed_at"] is None
            and (r["product"] or "").upper() != get_settings().intake_product.upper())


def _rows() -> list[Option]:
    rows = _cache["rows"]
    if rows is not None:
        return rows
    with _cache_lock:
        if _cache["rows"] is None:
            _cache["rows"] = [
                Option(id=r["id"], kind=r["kind"], product=r["product"], parent=r["parent"],
                       value=r["value"], description=r["description"],
                       conclusion=r["conclusion"], hidden=r["hidden"],
                       is_default=r["is_default"],
                       is_new=(r["seen_at"] is None and r["description"] is None
                               and r["removed_at"] is None
                               and (not r["hidden"] or _waiting_for_description(r))),
                       removed=r["removed_at"] is not None)
                for r in postgres.query(
                    "SELECT * FROM agent_options ORDER BY kind, product NULLS FIRST, "
                    "CASE WHEN kind = 'subtype' THEN parent END NULLS FIRST, "
                    "position, lower(value)")]
        return _cache["rows"]


def everything() -> list[Option]:
    """Every row, hidden and removed included: for the Values page."""
    return list(_rows())


def new_count() -> int:
    """How many values arrived and nobody has looked at yet."""
    return sum(1 for o in _rows() if o.is_new)


def products() -> list[Option]:
    """The products the agent may choose from."""
    return [o for o in _rows() if o.kind == "product" and o.offered]


def product(name_or_code: str) -> Option | None:
    key = name_or_code.strip().lower()
    return next((o for o in _rows() if o.kind == "product"
                 and key in (o.value.lower(), (o.product or "").lower())), None)


def queues(code: str) -> list[Option]:
    return [o for o in _rows() if o.kind == "queue" and o.offered
            and (o.product or "") == code.upper()]


def subtypes(code: str, type_name: str) -> list[Option]:
    """The subtypes the agent may pick under a type.

    Where a product's catalog gives reasons, a subtype without one is left
    out: their screen expects the reason from the catalog in Conclusions,
    and a subtype with none would leave it empty."""
    code = code.upper()
    of_product = [o for o in _rows() if o.kind == "subtype" and o.offered
                  and (o.product or "") == code]
    with_reasons = any(o.conclusion for o in of_product)
    return [o for o in of_product
            if (o.parent or "").lower() == type_name.strip().lower()
            and (o.conclusion or not with_reasons)]


def types(code: str) -> list[TypeChoice]:
    """The product's types, each with its subtypes. A type with no subtype
    left to pick is still offered: the ticket can carry a type alone."""
    code = code.upper()
    return [TypeChoice(o, subtypes(code, o.value)) for o in _rows()
            if o.kind == "type" and o.offered and (o.product or "") == code]


def statuses() -> list[Option]:
    return [o for o in _rows() if o.kind == "next_state" and o.offered]


def priorities() -> list[Option]:
    return [o for o in _rows() if o.kind == "priority" and o.offered]


def default(kind: str, code: str | None = None) -> Option | None:
    """The default value of a kind (for a product, for queues and types), when
    one is set and still offered."""
    want = (code or "").upper() if kind in ("queue", "type") else ""
    return next((o for o in _rows() if o.kind == kind and o.is_default and o.offered
                 and (o.product or "") == want), None)


def find(kind: str, value: str, *, product: str | None = None,
         parent: str | None = None) -> Option | None:
    """An offered value by name, ignoring case: for checking what the model
    returned. A subtype is looked up under its product and type."""
    v = value.strip().lower()
    for o in _rows():
        if o.kind != kind or not o.offered or o.value.lower() != v:
            continue
        if product is not None and (o.product or "") != product.upper():
            continue
        if parent is not None and (o.parent or "").lower() != parent.strip().lower():
            continue
        return o
    return None


# ---------------------------------------------------------------------------
# what people change
# ---------------------------------------------------------------------------

def _label(o: dict) -> str:
    parts = [o["product"], o["parent"] if o["kind"] == "subtype" else None, o["value"]]
    return " / ".join(p for p in parts if p)


def _get(option_id: int) -> dict:
    row = postgres.query_one("SELECT * FROM agent_options WHERE id = %s", (option_id,))
    if row is None:
        raise OptionError("no such value")
    return row


def describe(option_id: int, text: str | None, *, who: str) -> Option:
    """Set or clear when to use a value. Writing one clears New."""
    row = _get(option_id)
    text = (text or "").strip() or None
    if text and len(text) > MAX_DESCRIPTION:
        raise OptionError(f"at most {MAX_DESCRIPTION} characters")
    if text == row["description"]:
        return _option(option_id)
    # A product that arrived hidden is offered once someone describes it.
    show = bool(text) and _waiting_for_description(row)
    postgres.execute(
        """
        UPDATE agent_options SET description = %s, updated_by = %s, updated_at = now(),
               seen_at = COALESCE(seen_at, now()),
               hidden = CASE WHEN %s THEN false ELSE hidden END
        WHERE id = %s
        """,
        (text, who, show, option_id))
    audit.record(who=who, action="change", what=f"{row['kind']} description",
                 name=_label(row), old=row["description"], new=text)
    invalidate()
    return _option(option_id)


def set_hidden(option_id: int, hidden: bool, *, who: str) -> Option:
    row = _get(option_id)
    if row["hidden"] == hidden:
        return _option(option_id)
    if hidden and row["is_default"]:
        raise OptionError(f"{row['value']} is the default {row['kind']}: "
                          "make another one the default first")
    postgres.execute(
        """
        UPDATE agent_options SET hidden = %s, updated_by = %s, updated_at = now(),
               seen_at = COALESCE(seen_at, now())
        WHERE id = %s
        """,
        (hidden, who, option_id))
    audit.record(who=who, action="change", what=row["kind"], name=_label(row),
                 old="hidden" if row["hidden"] else "offered",
                 new="hidden" if hidden else "offered")
    invalidate()
    return _option(option_id)


def set_default(option_id: int, *, who: str) -> Option:
    """Make a value its kind's default (for its product). It replaces the one
    before: there is always exactly one, so a default is changed, never just
    taken away."""
    row = _get(option_id)
    if row["kind"] not in DEFAULT_KINDS:
        raise OptionError(f"a {row['kind']} cannot be a default")
    if row["is_default"]:
        return _option(option_id)
    if row["hidden"] or row["removed_at"] is not None:
        raise OptionError("a hidden or removed value cannot be the default")
    before = postgres.query_one(
        "SELECT value FROM agent_options WHERE kind = %s AND coalesce(product, '') = %s "
        "AND is_default", (row["kind"], row["product"] or ""))
    with postgres.connection() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE agent_options SET is_default = false "
                        "WHERE kind = %s AND coalesce(product, '') = %s AND is_default",
                        (row["kind"], row["product"] or ""))
            cur.execute("UPDATE agent_options SET is_default = true, updated_by = %s, "
                        "seen_at = COALESCE(seen_at, now()) WHERE id = %s", (who, option_id))
    audit.record(who=who, action="change", what=f"default {row['kind']}",
                 name=row["product"] or row["kind"],
                 old=before["value"] if before else None, new=row["value"])
    invalidate()
    return _option(option_id)


def mark_seen(option_id: int) -> Option:
    """Clear New without writing a description. Not audited: nothing about
    what the agent may choose changes."""
    _get(option_id)
    postgres.execute("UPDATE agent_options SET seen_at = COALESCE(seen_at, now()) "
                     "WHERE id = %s", (option_id,))
    invalidate()
    return _option(option_id)


def _option(option_id: int) -> Option:
    return next(o for o in _rows() if o.id == option_id)


def guidance() -> dict[str, str]:
    """Per kind, general advice to the agent on choosing it."""
    return {r["kind"]: r["guidance"] for r in
            postgres.query("SELECT kind, guidance FROM agent_option_kinds")}


def set_guidance(kind: str, text: str | None, *, who: str) -> None:
    if kind not in KINDS:
        raise OptionError(f"unknown kind {kind!r}")
    text = (text or "").strip() or None
    if text and len(text) > MAX_GUIDANCE:
        raise OptionError(f"at most {MAX_GUIDANCE} characters")
    old = guidance().get(kind)
    if text == old:
        return
    if text is None:
        postgres.execute("DELETE FROM agent_option_kinds WHERE kind = %s", (kind,))
    else:
        postgres.execute(
            """
            INSERT INTO agent_option_kinds (kind, guidance, updated_by) VALUES (%s, %s, %s)
            ON CONFLICT (kind) DO UPDATE SET guidance = EXCLUDED.guidance,
                updated_by = EXCLUDED.updated_by
            """,
            (kind, text, who))
    audit.record(who=who, action="change", what="guidance", name=kind, old=old, new=text)
