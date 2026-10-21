"""Configuration. Every environment variable the system reads, in one place.

Nothing else in the codebase calls os.getenv. Values are loaded from the .env
file in the support_agent folder, or from real environment variables in
deployment, which take precedence.

Two kinds of field below. The first block mirrors .env one for one. The second
holds values code reads but nobody sets per environment; adding the variable to
.env overrides any of them.

Most of these can also be changed on our console's Settings page, which only
our own team sees. A value saved there is kept in the database (passwords and
keys encrypted) and wins over .env; what the page cannot change -- the
database, the session secret, API_KEY -- stays in .env. get_settings()
returns the merged result; env_settings() returns .env alone, for what must be
known before the database can be read. See support_agent.settings_store.

Two kinds of people use the agent. Staff and admins of the T2 Ticketing
System work in their own system; its server calls our API with API_KEY and
names the person and what their role allows. Our own team signs in to our
console, with accounts of its own (console_users).

One deployment serves every product. All email reaches the T2 Ticketing System
through one mailbox and lands in one intake (product "Unclassified Email");
the agent chooses the product first, then the queue, type and subtype within
it. Per-product prompts live in products/<code>/; the values the agent may
choose live in the agent_options table (support_agent.options).

This module does not talk to the network itself. It only describes how to.
"""

from __future__ import annotations

import logging
import threading
import time
from enum import Enum
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import SecretStr, ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# .../support_agent/src/support_agent/config.py -> .../support_agent
REPO_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=REPO_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ================= set in .env =================

    # ---- the agent -----------------------------------------------------
    agent_name: str = "Support Agent"
    """In the headings of the notes it writes on tickets, the weekly report
    and the console."""

    history_products: str = "RICH,OLE5"
    """Comma-separated product codes. Whose closed tickets the Friday run adds
    to the history."""

    kb_tenants: str | None = None
    """Comma-separated code=index pairs, only for a product whose knowledge
    base index is not named after its code. Otherwise a product's index is
    its code in lower case: RICH -> rich, AVL-SME -> avl-sme."""

    precedent_tenant: str = "t2-precedent"
    """The similar-tickets index in Qdrant, shared by every product."""
    spam_tenant: str = "t2-spam"
    """The spam index in Qdrant. Spam arrives through the same mailbox for
    every product, so there is one."""


    # ---- database ------------------------------------------------------
    database_url: SecretStr
    """Used by the application."""

    database_url_direct: SecretStr | None = None
    """Migrations need a real session for the advisory lock. Unset: the same
    as DATABASE_URL, which is right for our own Postgres."""

    # ---- the T2 Ticketing System ---------------------------------------
    # Unset means the client is unconfigured: nothing is picked up or written.
    t2_base_url: str | None = None
    """Up to and including /TicketAPI, e.g. https://ticketsystem.t2.sa/TicketAPI
    or, against a local copy, http://localhost:5041/TicketAPI."""
    t2_user: str | None = None
    t2_password: SecretStr | None = None

    # ---- groq ----------------------------------------------------------
    groq_api_key: SecretStr | None = None
    groq_model: str = "openai/gpt-oss-120b"

    # Mail, for the urgent emails, sign-up codes and the weekly report.
    # Sending follows DRY_RUN: with it on, emails are composed and recorded
    # but not sent. With no SMTP_HOST, nothing is sent either.
    smtp_host: str | None = None
    smtp_port: int = 587
    mail_from: str | None = None
    mail_username: str | None = None
    mail_password: SecretStr | None = None
    ticket_url: str | None = None
    """A ticket's page in the ticket system, with {id} for its ID, e.g.
    https://ticketsystem.t2.sa/Tickets/Details/{id}. When set, urgent emails
    and the weekly report link straight to the ticket."""

    # The weekly history update (support_agent.history.weekly): Friday 04:00, Riyadh.
    weekly_enabled: bool = True
    weekly_report_recipients: str | None = None
    """Comma-separated addresses for the weekly report. Empty: the run still
    happens and the report is kept in weekly_runs, but nobody is emailed."""

    history_candidate_pool: int = 10
    """Candidates the reranker scores in the similar-tickets and spam searches.
    The reranker runs on CPU at about a second per candidate, so this is most
    of a search's time. The spam calibration is measured with the same number;
    keep them the same. The knowledge base has its own, kb_candidate_pool."""

    # The spam check (support_agent.history.spam). spam_threshold is the
    # similarity a ticket must reach against the spam index; set it from
    # `python scripts/spam.py calibrate`, never by guess -- a false positive
    # marks a real customer's ticket as spam.
    spam_threshold: float = 0.90
    spam_floor: float = 0.30
    spam_margin: float = 0.25
    """A ticket whose best spam match reaches spam_floor and beats its best
    real match by spam_margin is flagged too: reworded spam of a known kind."""
    spam_sender_min: int = 5
    """Spam tickets a domain must have sent, with no real ones, before the
    sender alone counts as a signal."""

    # The journey extraction over closed tickets (support_agent.history.journey),
    # through the same Groq client and key. The fallback answers when the main
    # model fails; each journey records which model wrote it.
    journey_model: str = "minimaxai/minimax-m2.7"
    journey_fallback_model: str = "openai/gpt-oss-120b"
    journey_timeout: float = 300.0
    journey_language: str = "English"
    """The language the journey's steps and summary are written in. They are
    read by reviewers and by the agent, never by a customer."""

    # ---- ragent2 -------------------------------------------------------
    qdrant_url: str | None = None
    docling_url: str | None = None
    upload_dir: Path = REPO_ROOT / "data" / "uploads"
    max_upload_mb: int = 25

    kb_candidate_pool: int = 10
    """How many candidates the reranker scores."""
    kb_top_k: int = 5
    kb_tool_repair_attempts: int = 1
    """How many times RAGent2 retries a malformed tool call from the model."""
    kb_max_rounds: int = 1
    kb_embed_timeout: float = 180.0
    """Seconds to wait for the embedding service on one request. RAGent2's own
    default is 60, which a long document's batch of chunks can exceed."""

    similar_limit: int = 5
    """How many similar past tickets are stored with each draft."""
    similar_min_score: float = 0.10
    """The lowest search score a past ticket needs to count as similar. Below
    it, the matches are what the search returns when nothing is really alike
    -- around 0.01 -- while related tickets have scored 0.15 and up."""

    # ---- our API ---------------------------------------------------------
    api_key: SecretStr | None = None
    """The key the ticket system's server sends to call our API. Unset: our
    API refuses every call. Long and random; never sent to a browser."""

    # ---- our console -----------------------------------------------------
    session_secret: SecretStr | None = None
    """Signs console sign-ins and encrypts the passwords saved on the Settings
    page. Changing it signs everyone out, and saved passwords must be entered
    again."""
    session_cookie: str = "t2_agent_console"

    # ---- behaviour -----------------------------------------------------
    dry_run: bool = True
    """When true, nothing is written to the ticket system.

    The draft is still built, still stored, still reviewable, and the write
    row is still created. Only the TicketUpdate call is skipped, so a dry run
    exercises everything except the one irreversible step.

    Defaults to TRUE deliberately: a missing or misspelled variable must not be
    able to write into their production ticket system. Turning it off is an
    explicit act.
    """

    log_level: str = "INFO"

    # ============ defaults; add to .env only to override ============

    env: Literal["dev", "staging", "prod"] = "dev"
    log_format: Literal["console", "json"] = "console"

    db_connect_timeout: int = 15
    db_pool_max: int = 5

    session_hours: int = 12

    t2_timeout: int = 30
    t2_poll_seconds: int = 60
    t2_verify_tls: bool = True
    """Off only for a local copy with a self-signed certificate. Never in
    production."""
    t2_min_interval: float = 0.6
    """Seconds between two calls. Their API allows 120 calls a minute from
    one address; this keeps us at 100 at most."""

    lookups_seconds: int = 300
    """How long the ticket system's values (TicketLookupsGet) are reused
    before they are read again. When the call fails, the last good answer is
    used, however old."""

    intake_product: str = "EMAIL"
    """The product code new email tickets carry until they are classified."""
    intake_states: str = "Open"
    """Comma-separated. The statuses an intake ticket can have; their Gmail
    sync opens every email ticket as Open."""
    intake_limit: int = 50
    """At most this many intake tickets per check."""

    timezone: str = "Asia/Riyadh"
    """The timezone the ticket system reports in. Its times are naive strings
    like "2026-10-19 08:04:28"; our columns are timestamptz, so the client
    attaches this zone when parsing them and converts to it when sending."""

    # ---- the server process --------------------------------------------
    host: str = "0.0.0.0"
    port: int = 8420

    poller_enabled: bool = True
    """Whether the server checks the intake for new tickets."""

    writer_enabled: bool = True
    writer_seconds: int = 30
    """How often approved drafts that were not written yet are retried.
    Approving already starts a write; this recovers the ones where the
    ticket system was down at the time."""

    migrate_on_start: bool = True
    migrations_dir: Path = REPO_ROOT / "migrations"

    prompts_dir: Path = REPO_ROOT / "prompts"
    """Prompts every product shares (choosing the product, the journeys)."""
    products_dir: Path = REPO_ROOT / "products"
    """One folder per product code (products/rich, products/ole5): the
    prompts for routing within that product."""

    # ---- validation ----------------------------------------------------
    @field_validator("*", mode="before")
    @classmethod
    def _blank_is_unset(cls, v: object) -> object:
        # T2_PASSWORD= in .env arrives as "", which would otherwise satisfy a
        # required field and produce a config that looks valid and is not.
        # An empty variable means unset, everywhere, without exception.
        if isinstance(v, str) and not v.strip():
            return None
        return v

    @field_validator("t2_base_url")
    @classmethod
    def _no_trailing_slash(cls, v: str | None) -> str | None:
        return v.rstrip("/") if v else v

    # ---- accessors -----------------------------------------------------
    @staticmethod
    def _split(text: str | None) -> list[str]:
        return [x.strip() for x in (text or "").split(",") if x.strip()]

    @property
    def history_product_list(self) -> list[str]:
        return [c.upper() for c in self._split(self.history_products)]

    @property
    def intake_state_list(self) -> list[str]:
        return self._split(self.intake_states)

    @property
    def kb_tenant_map(self) -> dict[str, str]:
        """Product code -> knowledge base index."""
        out: dict[str, str] = {}
        for pair in self._split(self.kb_tenants):
            code, sep, tenant = pair.partition("=")
            if sep and code.strip() and tenant.strip():
                out[code.strip().upper()] = tenant.strip()
        return out

    def kb_tenant(self, code: str) -> str:
        """The knowledge base index for a product: its code in lower case,
        unless KB_TENANTS names another."""
        code = code.strip().upper()
        return self.kb_tenant_map.get(code) or code.lower()

    def product_dir(self, code: str) -> Path:
        """products/<code>, lower case: products/rich for RICH."""
        return self.products_dir / code.strip().lower()

    @property
    def db_dsn(self) -> str:
        return self.database_url.get_secret_value()

    @property
    def db_dsn_direct(self) -> str:
        if self.database_url_direct is None:
            return self.db_dsn
        return self.database_url_direct.get_secret_value()

    @property
    def db_dsn_yoyo(self) -> str:
        """The direct DSN, scheme-annotated for yoyo.

        yoyo maps postgresql:// to psycopg2, which we do not install. Only yoyo
        needs the annotation, so only yoyo sees it.
        """
        dsn = self.db_dsn_direct
        # A time limit on connecting: without one, a connection that is
        # accepted and never answered (seen with localhost on Windows) waits
        # forever instead of failing.
        if "connect_timeout=" not in dsn:
            dsn += ("&" if "?" in dsn else "?") + f"connect_timeout={self.db_connect_timeout}"
        for prefix in ("postgresql://", "postgres://"):
            if dsn.startswith(prefix):
                return "postgresql+psycopg://" + dsn[len(prefix):]
        return dsn

    @property
    def tickets_configured(self) -> bool:
        """False until the ticket system's address, user and password are set."""
        return bool(self.t2_base_url and self.t2_user and self.t2_password)

    def redacted(self) -> dict:
        """The config as something safe to print. Secrets become a length."""
        out: dict = {}
        for name in type(self).model_fields:
            value = getattr(self, name)
            if isinstance(value, SecretStr):
                raw = value.get_secret_value()
                out[name] = f"<set, {len(raw)} chars>" if raw else None
            elif isinstance(value, Enum):
                out[name] = value.value
            elif isinstance(value, Path):
                out[name] = str(value)
            else:
                out[name] = value
        return out

    def warnings(self) -> list[str]:
        """Legal, but usually a mistake. Printed by the health check."""
        w: list[str] = []
        if not self.dry_run:
            w.append("DRY_RUN=false: this process CAN write into the ticket system")
        if not self.tickets_configured:
            w.append("Ticket system not configured (T2_BASE_URL, T2_USER, T2_PASSWORD): "
                     "nothing is picked up or written")
        elif not self.t2_base_url.startswith("https://") and "localhost" not in self.t2_base_url:
            w.append("T2_BASE_URL is not https: the password travels in the clear")
        if not self.t2_verify_tls:
            w.append("T2_VERIFY_TLS=false: the ticket system's certificate is not checked")
        if not self.session_secret:
            w.append("SESSION_SECRET is unset: nobody can sign in to the console")
        if not self.api_key:
            w.append("API_KEY is unset: the ticket system cannot call our API")
        elif len(self.api_key.get_secret_value()) < 32:
            w.append("API_KEY is shorter than 32 characters")
        if not self.prompts_dir.is_dir():
            w.append(f"No prompts at {self.prompts_dir}")
        for code in self.history_product_list:
            if not self.product_dir(code).is_dir():
                w.append(f"HISTORY_PRODUCTS has {code} but there is no {self.product_dir(code)}")
        return w


class ConfigError(RuntimeError):
    pass


@lru_cache(maxsize=1)
def env_settings() -> Settings:
    """.env and the environment alone, loaded once. For what has to be known
    before the database can be read -- the database itself above all. Raises
    ConfigError with a readable list of problems."""
    try:
        return Settings()  # type: ignore[call-arg]
    except ValidationError as exc:
        problems = "\n".join(
            f"  - {'.'.join(str(p) for p in e['loc']).upper()}: {e['msg']}"
            for e in exc.errors()
        )
        raise ConfigError(
            f"Configuration is incomplete.\n{problems}\n\n"
            f"Expected a .env file at: {REPO_ROOT / '.env'}"
        ) from None


# ---------------------------------------------------------------------------
# .env with the Settings page on top
# ---------------------------------------------------------------------------

OVERLAY_SECONDS = 15.0
"""How long the merged settings are reused before the database is read again.
A save on the Settings page refreshes them at once in the process that saved;
other processes (a script running alongside) see it within this long."""

RETRY_AFTER_FAILURE = 60.0
"""When the database could not be read, how long before trying again."""

_overlay: dict = {"at": float("-inf"), "settings": None}
_lock = threading.Lock()
_loading = threading.local()


def get_settings() -> Settings:
    """The settings in force: .env, with whatever the Settings page saved on
    top. Never raises for the database: when it cannot be read (not started,
    table not there yet), the last good result is used, or .env alone."""
    base = env_settings()
    if getattr(_loading, "busy", False):
        return base            # the database is being read for the overlay itself
    now = time.monotonic()
    cached = _overlay["settings"]
    if cached is not None and now - _overlay["at"] < OVERLAY_SECONDS:
        return cached
    if not _lock.acquire(blocking=False):
        return cached or base  # another thread is reading it right now
    try:
        _loading.busy = True
        from support_agent import settings_store
        merged = settings_store.apply(base)
    except Exception as exc:
        logging.getLogger(__name__).warning(
            "settings page values not read (%s); using %s", str(exc).splitlines()[0][:160],
            "the last ones" if cached else ".env")
        merged = cached or base
        now += RETRY_AFTER_FAILURE - OVERLAY_SECONDS   # not again for a minute
    finally:
        _loading.busy = False
        _lock.release()
    _overlay.update(at=now, settings=merged)
    return merged


def refresh_settings() -> None:
    """Read the Settings page values again on the next get_settings()."""
    _overlay["at"] = float("-inf")
