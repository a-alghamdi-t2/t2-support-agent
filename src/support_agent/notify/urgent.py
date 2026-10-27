"""Urgent tickets: noticing them, and telling the people who need to know.

Two signals, kept apart so everyone can see which one fired:

    keyword   a word or phrase from the list appears in the email: a rule
              someone wrote, checked here in code
    agent     the agent judged it urgent from what the email says, with a
              one-line reason: a model's call, and the email says so

Keywords and recipients are per product, plus ones for every product (product
NULL): a RiCH ticket is checked against RiCH's words and the shared ones, and
the alert goes to RiCH's people and the shared ones.

check() runs on every new draft, right after it is saved and before anyone has
reviewed it. Either signal marks the draft urgent (draft_urgent) and emails the
recipients, once per ticket a day: a ticket drafted again does not alert
again. Nothing here may stop a draft: every failure is caught, logged and
recorded against the email it concerns.
"""

from __future__ import annotations

import re
from datetime import timedelta

from support_agent import options
from support_agent.config import get_settings
from support_agent.db import audit, postgres
from support_agent.logging import get_logger
from support_agent.notify import mail

log = get_logger(__name__)

MESSAGE_CHARS = 600
ALERT_AGAIN_AFTER = timedelta(hours=24)
KEYWORD_MAX = 100


class UrgentError(ValueError):
    """A change that cannot be made. The message is for the person."""


# ---------------------------------------------------------------------------
# matching
# ---------------------------------------------------------------------------

_DIACRITICS = re.compile("[ً-ْٰـ]")    # harakat, tatweel
_ALEF = re.compile("[آأإٱ]")           # آ أ إ ٱ -> ا
_LATIN = re.compile(r"[A-Za-z]")


def normalise(text: str) -> str:
    """Compare text the way a person would: capitals, Arabic diacritics and
    the common spelling variants (أ/إ/آ, ى/ي, ة/ه) do not change a word."""
    t = _DIACRITICS.sub("", (text or "").casefold())
    t = _ALEF.sub("ا", t).replace("ى", "ي").replace("ة", "ه")
    return " ".join(t.split())


def matching_keywords(text: str, words: list[str]) -> list[str]:
    """The keywords that appear in the text.

    A keyword with Latin letters must appear as a whole word, so "down" does
    not fire on "download". An Arabic keyword may appear inside a word, since
    Arabic attaches و and ال to the front: "متوقف" is found in "والمتوقف".
    """
    hay = normalise(text)
    found = []
    for word in words:
        needle = normalise(word)
        if not needle:
            continue
        if _LATIN.search(needle):
            if re.search(r"(?<!\w)" + re.escape(needle) + r"(?!\w)", hay):
                found.append(word)
        elif needle in hay:
            found.append(word)
    return found


# ---------------------------------------------------------------------------
# the lists
# ---------------------------------------------------------------------------

def _code(code: str | None) -> str | None:
    if not code:
        return None
    o = options.product(code)
    if o is None or o.removed or (o.product or "").upper() == \
            get_settings().intake_product.upper():
        raise UrgentError(f"{code!r} is not a product")
    return o.product


def _who(who: str) -> str:
    who = (who or "").strip().lower()
    if "@" not in who[1:]:
        raise UrgentError("say who is doing this: an email")
    return who


def keywords(code: str | None, *, shared: bool = True) -> list[dict]:
    """The words in use for a product (and, with shared, the ones for every
    product), or only the shared ones (code None)."""
    if code is None:
        where, params = "product IS NULL", ()
    elif shared:
        where, params = "(product IS NULL OR upper(product) = %s)", (code.upper(),)
    else:
        where, params = "upper(product) = %s", (code.upper(),)
    return postgres.query(f"SELECT * FROM urgent_keywords WHERE active AND {where} "
                          "ORDER BY product NULLS FIRST, lower(keyword)", params)


def add_keyword(code: str | None, word: str, *, who: str) -> dict:
    who, code = _who(who), _code(code)
    word = " ".join((word or "").split())
    if not word:
        raise UrgentError("the keyword is empty")
    if len(word) > KEYWORD_MAX:
        raise UrgentError(f"at most {KEYWORD_MAX} characters")
    if any(normalise(k["keyword"]) == normalise(word) for k in keywords(code, shared=False)):
        raise UrgentError(f"{word!r} is already a keyword for {code or 'all products'}")
    row = postgres.query_one(
        """
        INSERT INTO urgent_keywords (product, keyword, added_by) VALUES (%s, %s, %s)
        ON CONFLICT (coalesce(product, ''), lower(btrim(keyword)))
        DO UPDATE SET active = true, keyword = EXCLUDED.keyword, added_by = EXCLUDED.added_by
        RETURNING *
        """, (code, word, who))
    audit.record(who=who, action="add", what="urgent keyword", name=code or "all products",
                 new=word)
    return row


def remove_keyword(keyword_id: int, *, who: str) -> None:
    """Switched off, not deleted, so the audit trail can still name it."""
    who = _who(who)
    row = postgres.query_one("SELECT * FROM urgent_keywords WHERE id = %s AND active",
                             (keyword_id,))
    if row is None:
        raise UrgentError("no such keyword")
    postgres.execute("UPDATE urgent_keywords SET active = false WHERE id = %s", (keyword_id,))
    audit.record(who=who, action="remove", what="urgent keyword",
                 name=row["product"] or "all products", old=row["keyword"])


def recipients(code: str | None, *, shared: bool = True) -> list[dict]:
    if code is None:
        where, params = "product IS NULL", ()
    elif shared:
        where, params = "(product IS NULL OR upper(product) = %s)", (code.upper(),)
    else:
        where, params = "upper(product) = %s", (code.upper(),)
    return postgres.query(f"SELECT * FROM urgent_recipients WHERE active AND {where} "
                          "ORDER BY product NULLS FIRST, lower(email)", params)


def add_recipient(code: str | None, email: str, name: str | None = None, *,
                  who: str) -> dict:
    who, code = _who(who), _code(code)
    address = (email or "").strip()
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", address):
        raise UrgentError("that is not an email address")
    label = " ".join((name or "").split()) or None
    if any(r["email"].lower() == address.lower() for r in recipients(code, shared=False)):
        raise UrgentError(f"{address} already gets {code or 'all products'}' alerts")
    row = postgres.query_one(
        """
        INSERT INTO urgent_recipients (product, email, name, added_by) VALUES (%s, %s, %s, %s)
        ON CONFLICT (coalesce(product, ''), lower(btrim(email)))
        DO UPDATE SET active = true, name = coalesce(EXCLUDED.name, urgent_recipients.name),
                      added_by = EXCLUDED.added_by
        RETURNING *
        """, (code, address, label, who))
    audit.record(who=who, action="add", what="urgent recipient", name=code or "all products",
                 new=address)
    return row


def remove_recipient(recipient_id: int, *, who: str) -> None:
    who = _who(who)
    row = postgres.query_one("SELECT * FROM urgent_recipients WHERE id = %s AND active",
                             (recipient_id,))
    if row is None:
        raise UrgentError("no such recipient")
    postgres.execute("UPDATE urgent_recipients SET active = false WHERE id = %s",
                     (recipient_id,))
    audit.record(who=who, action="remove", what="urgent recipient",
                 name=row["product"] or "all products", old=row["email"])


# ---------------------------------------------------------------------------
# a new draft
# ---------------------------------------------------------------------------

def _context(draft_id: int) -> dict | None:
    return postgres.query_one(
        """
        SELECT d.id AS draft_id, d.action, d.product, d.queue, d.summary,
               t.id AS ticket_id, t.t2_ticket_id, t.ticket_number, t.title, t.requester_email,
               (SELECT a.body_clean FROM articles a
                WHERE a.ticket_id = t.id AND a.party = 'request'
                ORDER BY a.article_no DESC NULLS LAST, a.id DESC LIMIT 1) AS message,
               u.by_agent, u.agent_reason
        FROM drafts d JOIN tickets t ON t.id = d.ticket_id
        LEFT JOIN draft_urgent u ON u.draft_id = d.id
        WHERE d.id = %s
        """, (draft_id,))


def _email(c: dict, found: list[str]) -> tuple[str, str]:
    product = options.product(c["product"]) if c["product"] else None
    product_name = product.value if product else (c["product"] or "unknown product")
    title = (c["title"] or "").strip() or "(no subject)"
    subject = f"[URGENT] {product_name}: {c['ticket_number']} {title}"[:200]
    why = []
    if found:
        why.append(("Keywords" if len(found) > 1 else "Keyword") + ": " + ", ".join(found))
    if c["by_agent"]:
        why.append("The agent judged it urgent (a model's call, not a rule): "
                   + (c["agent_reason"] or "no reason given"))
    message = (c["message"] or "").strip()
    if len(message) > MESSAGE_CHARS:
        message = message[:MESSAGE_CHARS] + " …"
    lines = [f"Ticket {c['ticket_number']} looks urgent.", "", "Why:",
             *[f"  - {w}" for w in why], "",
             f"Product:  {product_name}",
             f"Proposed: {c['queue'] or '-'}",
             f"Customer: {c['requester_email'] or 'unknown'}",
             f"Subject:  {title}", "", "Their message:", message or "(no text)", ""]
    url = get_settings().ticket_url
    if url and "{id}" in url:
        lines.append("The ticket: " + url.replace("{id}", c["t2_ticket_id"]))
    lines.append("Sent by the support agent. Nothing has been sent to the customer; "
                 "the proposal is waiting for review.")
    return subject, "\n".join(lines)


def _alerted_lately(ticket_id: int) -> bool:
    return postgres.query_one(
        "SELECT 1 AS x FROM urgent_emails WHERE ticket_id = %s AND status IN ('sent', 'dry_run') "
        "AND created_at > now() - %s LIMIT 1", (ticket_id, ALERT_AGAIN_AFTER)) is not None


def check(draft_id: int) -> dict | None:
    """Check a new draft. Marks it urgent and emails when either signal fires.
    Returns what fired and who was emailed, or None. Never raises."""
    try:
        c = _context(draft_id)
        if c is None or c["action"] == "spam":
            return None
        words = [k["keyword"] for k in keywords(c["product"])] if c["product"] else \
            [k["keyword"] for k in keywords(None)]
        found = matching_keywords(f"{c['title'] or ''}\n{c['message'] or ''}", words)
        if not found and not c["by_agent"]:
            return None
        postgres.execute(
            """
            INSERT INTO draft_urgent (draft_id, by_keyword, keywords) VALUES (%s, %s, %s)
            ON CONFLICT (draft_id) DO UPDATE SET by_keyword = EXCLUDED.by_keyword,
                keywords = EXCLUDED.keywords
            """, (draft_id, bool(found), found))
        result = {"keywords": found, "by_agent": bool(c["by_agent"]), "emailed": []}
        if _alerted_lately(c["ticket_id"]):
            result["skipped"] = "alerted about this ticket in the last day"
            return result
        subject, body = _email(c, found)
        people = recipients(c["product"]) if c["product"] else recipients(None)
        seen: set[str] = set()
        for p in people:
            address = p["email"].strip().lower()
            if address in seen:
                continue
            seen.add(address)
            status, error, message_id = "sent", None, None
            try:
                msg = mail.compose(p["email"], subject, body)
                message_id = msg["Message-ID"]
                status = "sent" if mail.deliver(msg) else "dry_run"
            except mail.NotConfigured as exc:
                status, error = "skipped", str(exc)
            except Exception as exc:
                status, error = "failed", f"{type(exc).__name__}: {exc}"[:500]
                log.exception("urgent email failed", extra={"draft": draft_id})
            postgres.execute(
                """
                INSERT INTO urgent_emails (draft_id, ticket_id, to_address, subject, body,
                                           status, error, rfc_message_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """, (draft_id, c["ticket_id"], p["email"], subject, body, status, error,
                      message_id))
            result["emailed"].append({"to": p["email"], "status": status})
        if not people:
            log.warning("urgent ticket but nobody to tell", extra={"draft": draft_id})
        log.info("urgent draft", extra={"draft": draft_id, "keywords": found,
                                        "by_agent": bool(c["by_agent"]),
                                        "emailed": len(result["emailed"])})
        return result
    except Exception:
        log.exception("urgent check failed", extra={"draft": draft_id})
        return None


def for_draft(draft_id: int) -> dict | None:
    """Why a draft is urgent and who was emailed: for the panel and the note."""
    row = postgres.query_one("SELECT by_keyword, keywords, by_agent, agent_reason, created_at "
                             "FROM draft_urgent WHERE draft_id = %s", (draft_id,))
    if row is None:
        return None
    row["emails"] = postgres.query("SELECT to_address, status, error, created_at "
                                   "FROM urgent_emails WHERE draft_id = %s ORDER BY id",
                                   (draft_id,))
    return row
