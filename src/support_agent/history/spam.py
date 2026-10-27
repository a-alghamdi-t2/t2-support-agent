"""Does this email look like the spam the team has already thrown away?

What the team threw away: closed tickets whose sender is Blocked -- their Mark
as Spam (or our Approve: Spam, through ContactMarkSpam) blocks the contact and
closes their tickets. Those go into their own index (SPAM_TENANT), built from
the same cleaned first message as the similar-tickets index, so spam can never
crowd out the precedents a reviewer needs.

Two signals, kept apart:

    text     how close the nearest spam tickets are, compared with how close
             the nearest real ticket is
    sender   whether this sender's company domain has sent spam and nothing
             real (never for free-mail domains: anyone has a Gmail address)

Neither closes anything. The check runs before step 1 and is shown to it, so
the agent can propose spam with a reason a reviewer can check; it is stored
with the draft (draft_spam). A person approves every spam proposal.

Messages sent many times -- the same offer every week -- are learned once:
each spam ticket is fingerprinted, and a copy of one already learned is marked
a repeat and left out of the index.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field

from support_agent.config import get_settings
from support_agent.db import postgres
from support_agent.history import index_sync
from support_agent.history.similar import MESSAGE_CHARS, MIN_TEXT
from support_agent.logging import get_logger

log = get_logger(__name__)

DOC_ID = "spam-tickets"

IS_SPAM = "coalesce(contact_status, '') = 'Blocked'"
IS_LEARNED = f"({IS_SPAM} AND spam_check = 'kept')"


def tenant() -> str:
    return get_settings().spam_tenant


@dataclass
class Match:
    ticket_number: str
    text: str
    score: float


@dataclass
class Verdict:
    score: float = 0.0
    """The best spam match's."""
    real_score: float = 0.0
    """The best real ticket's, for comparison."""
    matches: list[Match] = field(default_factory=list)
    sender_spam: int = 0
    sender_real: int = 0
    domain: str | None = None
    flagged: bool = False
    reason: str = ""
    checked: bool = False
    """False when there was nothing to check against (no spam learned yet)."""

    def block(self) -> str | None:
        """What step 1 is shown. Only when there is something to say."""
        if not self.checked or not (self.flagged or self.matches or self.sender_spam):
            return None
        lines = ["SPAM CHECK (from emails the team marked as spam before)"]
        if self.flagged:
            lines.append(f"  Looks like spam: {self.reason}")
        elif self.matches:
            lines.append(f"  Closest spam: {self.score:.2f}; closest real ticket: "
                         f"{self.real_score:.2f}. Not enough to call it spam.")
        if self.domain and (self.sender_spam or self.sender_real):
            lines.append(f"  {self.domain}: {self.sender_spam} spam, "
                         f"{self.sender_real} real tickets before")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# the index
# ---------------------------------------------------------------------------

def wanted() -> dict[str, dict]:
    rows = postgres.query(
        f"""
        SELECT ticket_number, title, search_text FROM closed_tickets
        WHERE {IS_LEARNED} AND length(coalesce(search_text, '')) >= %s
        """, (MIN_TEXT,))
    return {r["ticket_number"]: index_sync.entry(
        r["search_text"][:MESSAGE_CHARS], r["ticket_number"], DOC_ID,
        title=r["title"] or "") for r in rows}


def sync(refresh: set[str] | None = None) -> dict:
    return index_sync.sync(tenant(), DOC_ID, wanted(), refresh)


# ---------------------------------------------------------------------------
# which spam tickets to learn from
# ---------------------------------------------------------------------------

_URL = re.compile(r"https?://\S+|www\.\S+|\S+@\S+")
_DIGITS = re.compile(r"[0-9٠-٩۰-۹]+")
_NONWORD = re.compile(r"[^\w]+")


def fingerprint(text: str | None) -> str | None:
    """The message without what changes between copies of it: numbers, links
    and addresses, punctuation, case and Arabic spelling variants."""
    from support_agent.notify.urgent import normalise

    t = normalise(_URL.sub(" ", text or ""))
    t = _DIGITS.sub(" ", t)
    t = " ".join(_NONWORD.sub(" ", t).replace("_", " ").split())
    if len(t) < 10:
        return None
    return hashlib.sha1(t.encode("utf-8")).hexdigest()


def check_pending() -> dict:
    """Mark each spam ticket not checked yet as kept or a repeat, oldest first,
    so the first copy of a repeated message is the one kept. A sender who was
    unblocked since has their tickets' marks cleared, so they leave the index."""
    postgres.execute(
        f"UPDATE closed_tickets SET spam_check = NULL, spam_check_ref = NULL, "
        f"spam_fingerprint = NULL, spam_checked_at = NULL "
        f"WHERE spam_check IS NOT NULL AND NOT ({IS_SPAM})")
    rows = postgres.query(
        f"SELECT id, ticket_number, search_text FROM closed_tickets "
        f"WHERE {IS_SPAM} AND spam_check IS NULL ORDER BY t2_closed_at NULLS FIRST, id")
    counts = {"kept": 0, "repeat": 0}
    for row in rows:
        fp = fingerprint(row["search_text"])
        twin = postgres.query_one(
            "SELECT ticket_number FROM closed_tickets WHERE spam_check = 'kept' "
            "AND spam_fingerprint = %s LIMIT 1", (fp,)) if fp else None
        verdict = "repeat" if twin else "kept"
        postgres.execute(
            "UPDATE closed_tickets SET spam_check = %s, spam_check_ref = %s, "
            "spam_fingerprint = %s, spam_checked_at = now() WHERE id = %s",
            (verdict, twin["ticket_number"] if twin else None, fp, row["id"]))
        counts[verdict] += 1
    return {"checked": len(rows), **counts}


# ---------------------------------------------------------------------------
# the check
# ---------------------------------------------------------------------------

def sender_counts(domain: str | None) -> tuple[int, int]:
    """(spam, real) closed tickets from this company domain. Free-mail
    domains count nothing: their senders have nothing in common."""
    from support_agent.orchestrator.product import FREE_MAIL

    if not domain or domain in FREE_MAIL:
        return (0, 0)
    row = postgres.query_one(
        f"""
        SELECT count(*) FILTER (WHERE {IS_SPAM}) AS spam,
               count(*) FILTER (WHERE NOT ({IS_SPAM})) AS real
        FROM closed_tickets
        WHERE split_part(lower(coalesce(requester_email, '')), '@', 2) = %s
        """, (domain,))
    return (row["spam"], row["real"]) if row else (0, 0)


def decide(spam_score: float, real_score: float) -> str | None:
    """Which text rule a pair of scores meets.

      duplicate  the best spam match reaches SPAM_THRESHOLD and beats the best
                 real one. The second half matters: the same message is
                 sometimes thrown away once and handled another time.
      closer     the best spam match reaches SPAM_FLOOR and beats the best
                 real one by SPAM_MARGIN: reworded spam of a known kind.
    """
    s = get_settings()
    if spam_score >= s.spam_threshold and spam_score > real_score:
        return "duplicate"
    if spam_score >= s.spam_floor and spam_score - real_score >= s.spam_margin:
        return "closer"
    return None


def check(text: str | None, sender: str | None) -> Verdict:
    """Does this email look like spam? Never raises: a failed check is an
    empty verdict, and an email is never called spam by accident."""
    from support_agent.history import similar

    s = get_settings()
    v = Verdict()
    try:
        domain = (sender or "").rsplit("@", 1)[-1].strip().lower() if "@" in (sender or "") \
            else None
        v.domain = domain
        v.sender_spam, v.sender_real = sender_counts(domain)
        if (text or "").strip() and index_sync.size(tenant()):
            hits = index_sync.search(tenant(), text, 3)
            v.matches = [Match(h["ticket_number"], h["text"][:300], h["score"]) for h in hits]
            v.score = v.matches[0].score if v.matches else 0.0
            if v.matches and index_sync.size(similar.tenant()):
                real = index_sync.search(similar.tenant(), text, 1)
                v.real_score = real[0]["score"] if real else 0.0
            v.checked = True
        v.checked = v.checked or bool(v.sender_spam or v.sender_real)

        reasons = []
        rule = decide(v.score, v.real_score) if v.matches else None
        if rule == "duplicate":
            reasons.append(f"almost the same as spam ticket {v.matches[0].ticket_number} "
                           f"(score {v.score:.2f})")
        elif rule == "closer":
            reasons.append(f"closer to spam ticket {v.matches[0].ticket_number} "
                           f"({v.score:.2f}) than to any real ticket ({v.real_score:.2f})")
        if v.sender_spam >= s.spam_sender_min and v.sender_real == 0:
            reasons.append(f"{domain} has sent {v.sender_spam} spam tickets and no real ones")
        v.flagged, v.reason = bool(reasons), "; ".join(reasons)
    except Exception:
        log.exception("spam check failed")
        return Verdict()
    return v


def save(draft_id: int, v: Verdict) -> None:
    """Store the check with the draft. Never raises."""
    if not v.checked:
        return
    try:
        postgres.execute(
            """
            INSERT INTO draft_spam (draft_id, score, matches, sender_spam, sender_real,
                                    flagged, reason, threshold)
            VALUES (%s, %s, %s::jsonb, %s, %s, %s, %s, %s)
            ON CONFLICT (draft_id) DO UPDATE SET score = EXCLUDED.score,
                matches = EXCLUDED.matches, sender_spam = EXCLUDED.sender_spam,
                sender_real = EXCLUDED.sender_real, flagged = EXCLUDED.flagged,
                reason = EXCLUDED.reason, threshold = EXCLUDED.threshold
            """,
            (draft_id, v.score,
             json.dumps([m.__dict__ for m in v.matches], ensure_ascii=False),
             v.sender_spam, v.sender_real, v.flagged, v.reason or None,
             get_settings().spam_threshold))
    except Exception:
        log.exception("could not store the spam check", extra={"draft": draft_id})


def for_draft(draft_id: int) -> dict | None:
    try:
        return postgres.query_one(
            "SELECT score, matches, sender_spam, sender_real, flagged, reason "
            "FROM draft_spam WHERE draft_id = %s", (draft_id,))
    except Exception:
        log.exception("could not read the spam check", extra={"draft": draft_id})
        return None
