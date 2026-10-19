"""The Settings page of our console: what our team changes without a restart.

Only our own team sees it (console_users); nobody in the ticket system does.
.env keeps what must be known before the database can be read, or that nobody
should change from a browser: the database, SESSION_SECRET, API_KEY, the
folders and ports. Everything in FIELDS can be set on the page instead. A
value saved there is a row in app_settings and wins over .env; "back to .env"
deletes the row and the .env value (or the default) applies again.

Passwords and keys are encrypted in the database with a key derived from
SESSION_SECRET, and never leave the server: the page shows whether one is set
and lets it be replaced, not read. Changing SESSION_SECRET makes them
unreadable; the page then says so and they are entered again.

Every change is checked as a whole before anything is written -- the same
validation .env goes through -- and each changed setting is one row in the
audit log, with its old and new value (for a secret: that it changed, not to
what).
"""

from __future__ import annotations

import base64
import hashlib
import importlib
import sys
from dataclasses import dataclass
from typing import Any

from pydantic import SecretStr, ValidationError

from support_agent.config import Settings, env_settings, refresh_settings
from support_agent.db import postgres
from support_agent.logging import get_logger

log = get_logger(__name__)


class SettingsError(ValueError):
    pass


@dataclass(frozen=True)
class Field:
    key: str
    group: str
    label: str
    help: str
    kind: str = "text"        # text | list | pairs | bool | int | float | secret
    danger: str | None = None  # asked to confirm before saving this change


FIELDS: tuple[Field, ...] = (
    # ---- the agent
    Field("agent_name", "Agent", "Agent name",
          "In the headings of the notes it writes on tickets and in the weekly report."),
    Field("history_products", "Agent", "History products", "Comma-separated product codes "
          "(RICH, OLE5 ...). Whose closed tickets the Friday run adds to the history.", "list"),

    # ---- switches
    Field("dry_run", "Switches", "Dry run", "On: nothing is written to the ticket system. "
          "Drafts are still made and approved; only the write is skipped.", "bool",
          danger="Turning dry run off lets approvals write into the ticket system."),
    Field("poller_enabled", "Switches", "Pick up new tickets",
          "Checks the intake for new email tickets and drafts them.", "bool"),
    Field("writer_enabled", "Switches", "Write approvals",
          "Writes approved drafts to the ticket system (still nothing while dry run is on).",
          "bool"),
    Field("weekly_enabled", "Switches", "Friday history update",
          "Adds the week's closed tickets to the history every Friday at 04:00.", "bool"),

    # ---- the ticket system
    Field("t2_base_url", "Ticket system", "Ticket API address",
          "Up to and including /TicketAPI."),
    Field("t2_user", "Ticket system", "API user", ""),
    Field("t2_password", "Ticket system", "API password", "", "secret"),
    Field("t2_poll_seconds", "Ticket system", "Seconds between checks",
          "How often the intake is checked.", "int"),
    Field("ticket_url", "Ticket system", "Ticket link",
          "A ticket's page, with {id} for its ID: the link in urgent emails and the "
          "weekly report."),

    # ---- AI
    Field("groq_api_key", "AI models", "Groq API key", "", "secret"),
    Field("groq_model", "AI models", "Agent model", "The model that drafts each ticket."),
    Field("journey_model", "AI models", "History model",
          "Writes the journeys and team-scope evidence from closed tickets."),
    Field("journey_fallback_model", "AI models", "History fallback model",
          "Used when the history model fails."),
    Field("journey_language", "AI models", "History language",
          "The language the journeys are written in."),

    # ---- indexes
    Field("kb_tenants", "Indexes", "Knowledge bases", "Comma-separated code=index pairs: "
          "each product's knowledge base in Qdrant, e.g. RICH=rich.", "pairs"),
    Field("precedent_tenant", "Indexes", "Similar-tickets index", "The name in Qdrant."),
    Field("junk_tenant", "Indexes", "Junk index", "The name in Qdrant."),

    # ---- checks
    Field("similar_min_score", "Checks", "Similar tickets: minimum score",
          "Past tickets scoring below this are not shown.", "float"),
    Field("junk_threshold", "Checks", "Junk: near-duplicate score", "", "float"),
    Field("junk_floor", "Checks", "Junk: closer-to-junk floor", "", "float"),
    Field("junk_margin", "Checks", "Junk: closer-to-junk margin", "", "float"),
    Field("junk_sender_min", "Checks", "Junk: sender rule",
          "Junk tickets a domain must have sent, with no real ones.", "int"),

    # ---- email
    Field("smtp_host", "Email", "Mail server", ""),
    Field("smtp_port", "Email", "Mail server port", "", "int"),
    Field("mail_username", "Email", "Mail user", ""),
    Field("mail_password", "Email", "Mail password", "", "secret"),
    Field("mail_from", "Email", "Sent from", ""),
    Field("weekly_report_recipients", "Email", "Weekly report recipients",
          "Comma-separated addresses.", "list"),
)
BY_KEY = {f.key: f for f in FIELDS}
GROUPS = tuple(dict.fromkeys(f.group for f in FIELDS))

ENV_ONLY = ("DATABASE_URL", "SESSION_SECRET", "API_KEY", "QDRANT_URL", "DOCLING_URL",
            "LOG_LEVEL")
"""Shown on the page as set or not, never changeable from it."""

# Built once per process from settings these depend on; rebuilt after a save
# that changes any of them.
_CACHED = (
    ("support_agent.history.journey", "_client", {"groq_api_key", "journey_timeout"}),
    ("support_agent.history.journey", "_prompt", {"journey_language"}),
    ("support_agent.knowledge.retriever", "_tenant", {"groq_api_key", "kb_tenants"}),
    ("support_agent.history.precedent", "_tenant", {"groq_api_key", "precedent_tenant",
                                                    "junk_tenant"}),
    ("support_agent.history.precedent", "_rag", {"groq_api_key"}),
)


# ---------------------------------------------------------------------------
# secrets
# ---------------------------------------------------------------------------

def _fernet():
    from cryptography.fernet import Fernet

    secret = env_settings().session_secret
    if not secret:
        raise SettingsError("SESSION_SECRET is not set in .env; passwords cannot be "
                            "stored safely without it")
    digest = hashlib.sha256(b"support-agent settings\0" +
                            secret.get_secret_value().encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def _encrypt(plain: str) -> str:
    return _fernet().encrypt(plain.encode("utf-8")).decode("ascii")


def _decrypt(token: str) -> str | None:
    """None when it cannot be read: SESSION_SECRET changed since it was saved."""
    from cryptography.fernet import InvalidToken

    try:
        return _fernet().decrypt(token.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError, SettingsError):
        return None


# ---------------------------------------------------------------------------
# reading
# ---------------------------------------------------------------------------

def _rows() -> dict[str, dict]:
    """The saved values. On a connection of its own with a short time limit,
    not the pool: this runs inside get_settings(), which must stay quick and
    must not open the pool (and its threads) in a process that only wanted a
    setting while the database is down."""
    import psycopg
    from psycopg.rows import dict_row

    try:
        with psycopg.connect(env_settings().db_dsn, connect_timeout=3,
                             row_factory=dict_row) as conn:
            rows = conn.execute("SELECT key, value, secret, updated_by, updated_at "
                                "FROM app_settings").fetchall()
    except Exception as exc:
        # Before the first migration there is no table: nothing has been saved.
        if "app_settings" in str(exc) and "does not exist" in str(exc):
            return {}
        raise
    return {r["key"]: r for r in rows if r["key"] in BY_KEY}


def _optional(key: str) -> bool:
    annotation = Settings.model_fields[key].annotation
    return type(None) in getattr(annotation, "__args__", ())


def _typed(key: str, text: str | None) -> Any:
    """A stored or typed value as the model takes it: "" means unset for a
    setting that may be unset."""
    if text is None or (text == "" and _optional(key)):
        return None
    if BY_KEY[key].kind in ("list", "pairs"):
        return ",".join(x.strip() for x in text.replace("\n", ",").split(",") if x.strip())
    return text


def _merge(base: Settings, overrides: dict[str, Any]) -> Settings:
    data = base.model_dump()
    data.update(overrides)
    return type(base).model_validate(data)


def apply(base: Settings) -> Settings:
    """`base` with the saved values on top. A stored value the model refuses
    (it was valid when saved, the rules changed since) is skipped and logged
    rather than taking every other setting down with it."""
    overrides: dict[str, Any] = {}
    for key, row in _rows().items():
        if row["secret"]:
            plain = _decrypt(row["value"])
            if plain is None:
                log.warning("saved secret cannot be read; using .env", extra={"setting": key})
                continue
            overrides[key] = plain or None
        else:
            overrides[key] = _typed(key, row["value"])
    if not overrides:
        return base
    try:
        return _merge(base, overrides)
    except ValidationError as exc:
        bad = {str(e["loc"][0]) for e in exc.errors() if e.get("loc")}
        log.warning("saved settings refused; using .env for them", extra={"settings": sorted(bad)})
        return _merge(base, {k: v for k, v in overrides.items() if k not in bad})


def _display(value: Any) -> Any:
    if isinstance(value, bool) or value is None or isinstance(value, (int, float)):
        return value
    if isinstance(value, SecretStr):
        return None
    return str(value)


def describe() -> dict:
    """Everything the Settings page shows. Never a secret's value."""
    from support_agent.config import get_settings

    base = env_settings()
    rows = _rows()
    current = get_settings()
    from_env = base.model_fields_set
    fields = []
    for f in FIELDS:
        row = rows.get(f.key)
        item = {"key": f.key, "group": f.group, "label": f.label, "help": f.help,
                "kind": f.kind, "danger": f.danger,
                "source": "page" if row else (".env" if f.key in from_env else "default"),
                "updated_by": row["updated_by"] if row else None,
                "updated_at": row["updated_at"].isoformat() if row else None}
        if f.kind == "secret":
            item["is_set"] = bool(getattr(current, f.key))
            item["unreadable"] = bool(row) and _decrypt(row["value"]) is None
        else:
            item["value"] = _display(getattr(current, f.key))
        fields.append(item)
    env_set = {name: bool(getattr(base, name.lower(), None)) for name in ENV_ONLY}
    return {"groups": list(GROUPS), "fields": fields, "env_only": env_set}


# ---------------------------------------------------------------------------
# saving
# ---------------------------------------------------------------------------

def _check_ranges(merged: Settings, changed: set[str]) -> list[str]:
    problems = []
    for key in ("similar_min_score", "junk_threshold", "junk_floor", "junk_margin"):
        if key in changed and not 0 <= getattr(merged, key) <= 1:
            problems.append(f"{BY_KEY[key].label}: between 0 and 1")
    if "t2_poll_seconds" in changed and merged.t2_poll_seconds < 10:
        problems.append("Seconds between checks: at least 10")
    if "t2_base_url" in changed and merged.t2_base_url and not merged.t2_base_url.startswith(
            ("https://", "http://")):
        problems.append("Ticket API address: starts with https://")
    if "kb_tenants" in changed and merged.kb_tenants:
        if any("=" not in p for p in merged.kb_tenants.split(",")):
            problems.append("Knowledge bases: write each as CODE=index, separated by commas")
    if "ticket_url" in changed and merged.ticket_url:
        if "{id}" not in merged.ticket_url or not merged.ticket_url.startswith(
                ("https://", "http://")):
            problems.append("Ticket link: a full address containing {id}")
    return problems


def save(changes: dict[str, Any], who: str) -> dict:
    """Save what the page sent: {key: value}, or {key: None} for "back to .env".

    For a secret, a value replaces it and None goes back to .env; it is never
    read back. Everything is checked together first; nothing is written if
    anything is wrong. `who` is the console user's email.
    """
    unknown = [k for k in changes if k not in BY_KEY]
    if unknown:
        raise SettingsError(f"not a setting the page can change: {', '.join(unknown)}")
    if not changes:
        return describe()

    from support_agent.config import get_settings

    before = get_settings()
    rows = _rows()
    # What will be in force: the saved values that stay, plus these changes.
    overrides: dict[str, Any] = {}
    for key, row in rows.items():
        if key in changes:
            continue
        overrides[key] = (_decrypt(row["value"]) if row["secret"] else _typed(key, row["value"]))
    resets, writes = set(), {}
    for key, value in changes.items():
        if value is None:
            resets.add(key)
            continue
        text = str(value).lower() if isinstance(value, bool) else str(value).strip()
        if BY_KEY[key].kind == "secret" and not text:
            resets.add(key)        # an empty password box means "nothing typed"
            continue
        writes[key] = text
        overrides[key] = _typed(key, text) if BY_KEY[key].kind != "secret" else text
    try:
        merged = _merge(env_settings(), {k: v for k, v in overrides.items() if v is not None
                                         or _optional(k)})
    except ValidationError as exc:
        raise SettingsError("; ".join(
            f"{BY_KEY[str(e['loc'][0])].label if str(e['loc'][0]) in BY_KEY else e['loc'][0]}"
            f": {e['msg']}" for e in exc.errors())) from None
    changed = set(writes) | resets
    problems = _check_ranges(merged, changed)
    if problems:
        raise SettingsError("; ".join(problems))

    with postgres.connection() as conn:
        with conn.cursor() as cur:
            for key in resets:
                cur.execute("DELETE FROM app_settings WHERE key = %s", (key,))
            for key, text in writes.items():
                secret = BY_KEY[key].kind == "secret"
                cur.execute(
                    """
                    INSERT INTO app_settings (key, value, secret, updated_by, updated_at)
                    VALUES (%s, %s, %s, %s, now())
                    ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value,
                        secret = EXCLUDED.secret, updated_by = EXCLUDED.updated_by,
                        updated_at = now()
                    """,
                    (key, _encrypt(text) if secret else text, secret, who))

    refresh_settings()
    after = get_settings()
    from support_agent.db import audit
    for key in sorted(changed):
        if BY_KEY[key].kind == "secret":
            old = "set" if getattr(before, key) else "not set"
            new = "replaced" if key in writes else "back to .env"
        else:
            old, new = _display(getattr(before, key)), _display(getattr(after, key))
            if key in resets:
                new = f"{new} (back to .env)"
        audit.record(who=who, action="change", what="setting", name=key, old=old, new=new)
    _rebuild(changed)
    log.info("settings changed", extra={"by": who, "keys": sorted(changed)})
    return describe()


def _rebuild(changed: set[str]) -> None:
    """Drop what was built from a setting that changed, so it is built again
    with the new value on next use."""
    for module, name, keys in _CACHED:
        if changed & keys and module in sys.modules:
            fn = getattr(importlib.import_module(module), name, None)
            if fn is not None and hasattr(fn, "cache_clear"):
                fn.cache_clear()
