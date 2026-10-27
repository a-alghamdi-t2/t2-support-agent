"""The weekly history update, and the report that goes with it.

Every Friday at 04:00 Riyadh time (the runtime's weekly loop asks maybe_run):

  1. Ask the ticket system for every ticket closed since the last run, in the
     history products (HISTORY_PRODUCTS) and the intake product -- spam is
     closed while still unclassified. The first run reads HISTORY_FIRST_DAYS
     back.
  2. For each: store it (closed_tickets), build its timeline and search text,
     and, unless it is spam, write its journey.
  3. Bring the similar-tickets and spam indexes in line, so the new tickets
     count from the next draft on.
  4. Write a report of the week -- what the agent drafted, what reviewers
     changed and why, how the tickets it handled ended up -- and email it to
     WEEKLY_REPORT_RECIPIENTS.

The report is built from specific tickets, not rates: every count sits beside
the list it counts, so each line can be checked in the ticket system. Rates
come last, as a summary of those lists.
"""

from __future__ import annotations

import json
from collections import Counter
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from support_agent.config import get_settings
from support_agent.db import postgres
from support_agent.logging import get_logger

log = get_logger(__name__)

RIYADH = ZoneInfo("Asia/Riyadh")
RUN_WEEKDAY = 4          # Friday (Monday is 0)
RUN_HOUR = 4             # 04:00 Riyadh time


# ---------------------------------------------------------------------------
# when
# ---------------------------------------------------------------------------

def week_key(when: datetime) -> str:
    iso = when.astimezone(RIYADH).isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def due(now: datetime | None = None) -> str | None:
    """This week's key if it is Friday 04:00 or later in Riyadh and the week has
    not been run yet. Later on Friday counts: if the app was down at 04:00, the
    run happens when it is back, the same day."""
    local = (now or datetime.now(UTC)).astimezone(RIYADH)
    if local.weekday() != RUN_WEEKDAY or local.hour < RUN_HOUR:
        return None
    key = week_key(local)
    done = postgres.query_one("SELECT 1 AS x FROM weekly_runs WHERE week_key = %s", (key,))
    return None if done else key


def window_start(end: datetime) -> datetime:
    """Where the last completed run stopped, or HISTORY_FIRST_DAYS back."""
    row = postgres.query_one("SELECT max(window_end) AS t FROM weekly_runs WHERE status = 'done'")
    if row and row["t"]:
        return row["t"]
    return end - timedelta(days=get_settings().history_first_days)


def maybe_run() -> dict:
    """Called by the runtime every few minutes: runs the week's update when due."""
    if not get_settings().weekly_enabled:
        return {"due": False, "enabled": False}
    key = due()
    return run(key) if key else {"due": False}


# ---------------------------------------------------------------------------
# the history
# ---------------------------------------------------------------------------

def boilerplate_loader():
    """The learned boilerplate per sender, loaded once each."""
    from support_agent.intake import search_text

    cache: dict = {}

    def bp(sender):
        key = (sender or "").strip().lower()
        if key not in cache:
            cache[key] = search_text.load_boilerplate(sender)
        return cache[key]
    return bp


def _save_timeline(cid: int, tl) -> None:
    from support_agent.history import timeline

    postgres.execute(
        """
        INSERT INTO ticket_timelines (closed_ticket_id, entries, queue_path, article_count,
                                      kept_count, history_count, noise_count, empty_count,
                                      clean_ver)
        VALUES (%s, %s::jsonb, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (closed_ticket_id) DO UPDATE SET
            entries = EXCLUDED.entries, queue_path = EXCLUDED.queue_path,
            article_count = EXCLUDED.article_count, kept_count = EXCLUDED.kept_count,
            history_count = EXCLUDED.history_count, noise_count = EXCLUDED.noise_count,
            empty_count = EXCLUDED.empty_count, clean_ver = EXCLUDED.clean_ver,
            fetched_at = now()
        """,
        (cid, json.dumps(tl.entries, ensure_ascii=False), tl.queue_path, tl.article_count,
         len([e for e in tl.entries if e["kind"] != "queue"]), tl.history_count,
         tl.noise_count, tl.empty_count, timeline.CLEAN_VER))


def _fill_search_text(cid: int, bp) -> None:
    """The first message, cleaned exactly as a new ticket's is, so the two can
    be compared on the problem."""
    from support_agent.intake import search_text

    row = postgres.query_one(
        "SELECT sender_address, body_raw FROM closed_articles WHERE closed_ticket_id = %s "
        "ORDER BY article_no NULLS LAST, id LIMIT 1", (cid,))
    if row is None:
        return
    text = search_text.build(row["body_raw"] or "", bp(row["sender_address"]),
                             sender=row["sender_address"])
    postgres.execute("UPDATE closed_tickets SET search_text = %s, search_ver = %s "
                     "WHERE id = %s", (text, search_text.SEARCH_VER, cid))


def add_one(t2, t2_ticket_id: str, bp) -> dict:
    """One closed ticket into the history: stored, timeline, search text. The
    same steps for the Friday run and for `scripts/weekly.py one`."""
    from support_agent.history import store, timeline

    ticket = t2.get(t2_ticket_id)
    stored = store.save(ticket)
    tl = timeline.build(ticket, bp)
    _save_timeline(stored.id, tl)
    _fill_search_text(stored.id, bp)
    return {"id": stored.id, "ticket_number": stored.ticket_number,
            "product": ticket.product or ticket.product_code, "queue": ticket.queue,
            "spam": stored.spam,
            "messages": stored.kept, "path": tl.queue_path,
            "has_messages": any(e["kind"] != "queue" for e in tl.entries)}


def journey_for(cid: int) -> str:
    from support_agent.history import journey

    return journey.for_ticket(cid)


def add_to_history(start: datetime, end: datetime, *, t2=None, journeys: bool = True) -> dict:
    """Bring the tickets closed in the window into the history."""
    from support_agent.history.store import history_products

    bp = boilerplate_loader()
    added: list[dict] = []
    failed: list[dict] = []
    own = t2 is None
    if own:
        from support_agent.ticketsystem import connect
        t2 = connect()
    try:
        ids = t2.closed_between(start, end, products=history_products())
        log.info("weekly: tickets closed in the window", extra={"count": len(ids)})
        for tid in ids:
            try:
                added.append(add_one(t2, tid, bp))
            except Exception as exc:
                failed.append({"t2_id": tid, "step": "read",
                               "error": f"{type(exc).__name__}: {exc}"[:300]})
                log.exception("weekly: reading a closed ticket failed", extra={"t2_id": tid})
    finally:
        if own:
            t2.close()

    written = 0
    for a in (x for x in added if journeys and not x["spam"] and x["has_messages"]):
        try:
            journey_for(a["id"])
            written += 1
        except Exception as exc:
            failed.append({"ticket_number": a["ticket_number"], "step": "journey",
                           "error": f"{type(exc).__name__}: {exc}"[:300]})
            log.exception("weekly: journey failed", extra={"ticket": a["ticket_number"]})
    return {"found": len(ids), "added": added, "journeys": written, "failed": failed}


def sync_indexes(added: list[dict]) -> dict:
    """Both indexes in line with the history: the week's tickets added, what
    should not be there removed. Synced every week, even with nothing new, so
    anything a past run left wrong is corrected."""
    from support_agent.history import similar, spam

    out: dict = {}
    if not get_settings().qdrant_url:
        out["skipped"] = "QDRANT_URL is not set"
        return out
    try:
        out["similar"] = similar.sync(refresh={a["ticket_number"] for a in added
                                               if not a["spam"]})
    except Exception as exc:
        out["similar_error"] = f"{type(exc).__name__}: {exc}"[:300]
        log.exception("weekly: similar index sync failed")
    try:
        out["spam_checked"] = spam.check_pending()
        out["spam"] = spam.sync(refresh={a["ticket_number"] for a in added if a["spam"]})
    except Exception as exc:
        out["spam_error"] = f"{type(exc).__name__}: {exc}"[:300]
        log.exception("weekly: spam index sync failed")
    return out


# ---------------------------------------------------------------------------
# the report
# ---------------------------------------------------------------------------

def _pct(n: int, d: int) -> str:
    return f"{round(100 * n / d)}%" if d else "-"


def gather(start: datetime, end: datetime, added: list[dict]) -> dict:
    """Everything the report says, as data: lists of tickets, not only counts."""
    drafted = postgres.query(
        """
        SELECT d.id, t.ticket_number, t.title,
               coalesce(d.reply_kind::text, d.action::text) AS decision, d.product, d.queue
        FROM drafts d JOIN tickets t ON t.id = d.ticket_id
        WHERE d.created_at >= %s AND d.created_at < %s ORDER BY d.id
        """, (start, end))
    reviews = postgres.query(
        """
        SELECT r.outcome::text AS outcome, r.changed_fields, r.changes, r.justification,
               r.comment, r.final, r.created_at, r.reviewed_by AS reviewer,
               d.id AS draft_id, coalesce(d.reply_kind::text, d.action::text) AS decision,
               d.product AS agent_product, d.queue AS agent_queue, t.ticket_number, t.title
        FROM reviews r JOIN drafts d ON d.id = r.draft_id JOIN tickets t ON t.id = d.ticket_id
        WHERE r.created_at >= %s AND r.created_at < %s ORDER BY r.created_at
        """, (start, end))
    urgent = postgres.query(
        """
        SELECT t.ticket_number, t.title, u.keywords, u.by_agent, u.agent_reason,
               (SELECT count(*) FROM urgent_emails e
                WHERE e.draft_id = d.id AND e.status = 'sent') AS emailed
        FROM draft_urgent u JOIN drafts d ON d.id = u.draft_id
        JOIN tickets t ON t.id = d.ticket_id
        WHERE d.created_at >= %s AND d.created_at < %s ORDER BY d.id
        """, (start, end))
    spam_flags = postgres.query(
        """
        SELECT t.ticket_number, t.title, d.action::text AS proposed, d.status::text AS status,
               j.flagged, j.reason,
               r.outcome::text AS outcome, r.final->>'action' AS approved_as
        FROM drafts d JOIN tickets t ON t.id = d.ticket_id
        LEFT JOIN draft_spam j ON j.draft_id = d.id
        LEFT JOIN reviews r ON r.draft_id = d.id
        WHERE (d.action = 'spam' OR j.flagged)
          AND d.created_at >= %s AND d.created_at < %s ORDER BY d.id
        """, (start, end))
    numbers = [a["ticket_number"] for a in added]
    outcomes = postgres.query(
        """
        SELECT DISTINCT ON (t.ticket_number)
               t.ticket_number, t.title, d.queue AS agent_queue,
               r.final->>'queue' AS sent_queue, r.outcome::text AS outcome,
               c.queue AS closed_queue, coalesce(tl.queue_path, '{}') AS path
        FROM tickets t
        JOIN drafts d ON d.ticket_id = t.id
        JOIN reviews r ON r.draft_id = d.id
        JOIN closed_tickets c ON c.ticket_number = t.ticket_number
        LEFT JOIN ticket_timelines tl ON tl.closed_ticket_id = c.id
        WHERE t.ticket_number = ANY(%s) AND r.outcome::text <> 'reject'
          AND coalesce(r.final->>'action', '') <> 'spam'
        ORDER BY t.ticket_number, r.created_at DESC
        """, (numbers,)) if numbers else []
    waiting = postgres.query(
        """
        SELECT DISTINCT ON (t.ticket_number) t.ticket_number, t.title,
               r.final->>'queue' AS sent_queue, r.created_at
        FROM reviews r JOIN drafts d ON d.id = r.draft_id JOIN tickets t ON t.id = d.ticket_id
        WHERE r.outcome::text <> 'reject' AND coalesce(r.final->>'action', '') <> 'spam'
          AND NOT EXISTS (SELECT 1 FROM closed_tickets c WHERE c.ticket_number = t.ticket_number)
        ORDER BY t.ticket_number, r.created_at DESC
        """)
    sizes = postgres.query_one(
        """
        SELECT count(*) FILTER (WHERE coalesce(contact_status, '') <> 'Blocked') AS real,
               count(*) FILTER (WHERE contact_status = 'Blocked') AS spam
        FROM closed_tickets
        """)
    return {"drafted": drafted, "reviews": reviews, "urgent": urgent,
            "spam_flags": spam_flags, "outcomes": outcomes, "waiting": waiting,
            "sizes": sizes}


def _stayed(row: dict) -> bool:
    """Did the ticket close in the queue it was sent to, without moving on?"""
    sent = row["sent_queue"] or row["agent_queue"]
    later = (row["path"] or [])[1:]
    return row["closed_queue"] == sent and all(q == sent for q in later)


def _path_text(row: dict) -> str:
    path = row["path"] or []
    closed = row["closed_queue"] or "?"
    return (" -> ".join(path) + f", closed in {closed}") if len(path) > 1 \
        else f"closed in {closed}"


def render(start: datetime, end: datetime, history: dict, indexes: dict, g: dict) -> str:
    a, b = start.astimezone(RIYADH), end.astimezone(RIYADH)
    L: list[str] = []
    w = L.append

    w("SUPPORT AGENT -- WEEKLY REPORT")
    w(f"{a:%d %b %Y %H:%M} to {b:%d %b %Y %H:%M} (Riyadh time)")
    w("")

    reviews = g["reviews"]
    as_drafted = [r for r in reviews if r["outcome"] == "approve"]
    edited = [r for r in reviews if r["outcome"] == "edit"]
    rejected = [r for r in reviews if r["outcome"] == "reject"]
    kinds = Counter(d["decision"] for d in g["drafted"])
    w("1. WHAT THE AGENT DID")
    w(f"   Drafts made: {len(g['drafted'])}"
      + (f"  ({', '.join(f'{n} {k}' for k, n in kinds.most_common())})" if kinds else ""))
    w(f"   Reviewed: {len(reviews)}  --  approved as drafted {len(as_drafted)}, "
      f"approved with changes {len(edited)}, rejected {len(rejected)}")
    w("")

    w(f"2. WHAT REVIEWERS CHANGED ({len(edited)})")
    if not reviews:
        w("   No reviews this week.")
    elif not edited:
        w("   Nothing: every approval went out as the agent drafted it.")
    for r in edited:
        w(f"   {r['ticket_number']}  {(r['title'] or '')[:70]}")
        changes = r["changes"] or {}
        for f in r["changed_fields"] or []:
            ch = changes.get(f) or {}
            if f == "reply_body":
                w("      reply: edited")
            elif isinstance(ch, dict):
                w(f"      {f}: {ch.get('from') or '-'}  ->  {ch.get('to') or '-'}")
        if r["comment"]:
            w(f"      comment: {r['comment'][:200]}")
        w(f"      reviewer: {r['reviewer']}")
    fields = Counter(f for r in edited for f in (r["changed_fields"] or []))
    if fields:
        w("   Most changed: " + ", ".join(f"{f} ({n})" for f, n in fields.most_common()))
    w("")

    w(f"3. REJECTED ({len(rejected)})")
    if not rejected:
        w("   None.")
    for r in rejected:
        w(f"   {r['ticket_number']}  {(r['title'] or '')[:70]}")
        w(f"      agent proposed: {r['decision']}, {r['agent_product'] or '-'} / "
          f"{r['agent_queue'] or '-'}")
        w(f"      why rejected: {(r['justification'] or '-')[:200]}  ({r['reviewer']})")
    w("")

    out = g["outcomes"]
    stayed = [o for o in out if _stayed(o)]
    w(f"4. HOW THE AGENT'S TICKETS ENDED UP -- closed this week ({len(out)})")
    if not out:
        w("   None of the tickets the agent drafted was closed this week.")
    for o in out:
        sent = o["sent_queue"] or o["agent_queue"]
        who = f"agent chose {o['agent_queue']}" + (
            f", reviewer sent it to {sent}" if sent != o["agent_queue"] else "")
        w(f"   {'OK   ' if _stayed(o) else 'MOVED'} {o['ticket_number']}  {who}; {_path_text(o)}")
    if out:
        agreed = [o for o in out if (o["sent_queue"] or o["agent_queue"]) == o["agent_queue"]]
        w(f"   Closed where it was sent: {len(stayed)} of {len(out)}. "
          f"Moved on to another team first: {len(out) - len(stayed)}.")
        w(f"   Reviewer kept the agent's queue: {len(agreed)} of {len(out)}.")
    w("")

    waiting = g["waiting"]
    w(f"5. STILL OPEN -- approved, not closed yet ({len(waiting)})")
    for r in waiting[:15]:
        age = (end - r["created_at"]).days
        w(f"   {r['ticket_number']}  sent to {r['sent_queue'] or '?'}, "
          f"{age} day{'s' if age != 1 else ''} ago")
    if len(waiting) > 15:
        w(f"   ... and {len(waiting) - 15} more.")
    if not waiting:
        w("   None.")
    w("")

    w(f"6. URGENT ({len(g['urgent'])})")
    for u in g["urgent"]:
        why = []
        if u["keywords"]:
            why.append("keyword: " + ", ".join(u["keywords"]))
        if u["by_agent"]:
            why.append("agent: " + (u["agent_reason"] or "no reason given")[:120])
        w(f"   {u['ticket_number']}  {(u['title'] or '')[:60]}")
        w(f"      {'; '.join(why)}  --  emailed {u['emailed']} person(s)")
    if not g["urgent"]:
        w("   None.")
    w("")

    sf = g["spam_flags"]
    w(f"7. SPAM ({len(sf)})")
    for j in sf:
        said = "proposed spam" if j["proposed"] == "spam" else "flagged by the check, not proposed"
        if j["outcome"] is None:
            # A draft withdrawn without a review: the ticket left the intake
            # first -- closed with the sender's other tickets, or handled on
            # their page.
            verdict = ("not reviewed yet" if j["status"] == "pending"
                       else "closed before review")
        elif j["outcome"] == "reject":
            verdict = "draft rejected"
        elif j["approved_as"] == "spam":
            verdict = "approved as spam"
        else:
            verdict = f"reviewer disagreed: {j['approved_as']}"
        w(f"   {j['ticket_number']}  {(j['title'] or '')[:60]}  --  {said}; {verdict}")
        if j["reason"]:
            w(f"      check: {j['reason'][:160]}")
    if not sf:
        w("   None.")
    w("")

    added = history["added"]
    per = Counter("spam" if x["spam"] else (x["product"] or "?") for x in added)
    w("8. HISTORY UPDATED")
    w(f"   Closed tickets added: {len(added)} of {history['found']} found"
      + (f"  ({', '.join(f'{q} {n}' for q, n in per.most_common())})" if per else ""))
    w(f"   Journeys written: {history['journeys']}")
    for label, key in (("Similar-tickets index", "similar"), ("Spam index", "spam")):
        if key in indexes:
            x = indexes[key]
            w(f"   {label}: {x['added']} added, {x['refreshed']} refreshed, "
              f"{x['removed']} removed -- now {x['total']} tickets")
    if "spam_checked" in indexes:
        c = indexes["spam_checked"]
        w(f"   Spam checked: {c['checked']} -- kept {c['kept']}, repeats {c['repeat']}")
    if "skipped" in indexes:
        w(f"   Indexes not updated: {indexes['skipped']}")
    w(f"   History now holds {g['sizes']['real']} real and {g['sizes']['spam']} spam tickets.")
    w("")

    problems = list(history["failed"])
    for k in ("similar_error", "spam_error"):
        if k in indexes:
            problems.append({"step": k.replace("_error", " index"), "error": indexes[k]})
    w(f"9. PROBLEMS ({len(problems)})")
    for p in problems:
        who = p.get("ticket_number") or p.get("t2_id") or ""
        w(f"   {p['step']} {who}: {p['error']}")
    if not problems:
        w("   None.")
    w("")

    w("SUMMARY")
    if reviews:
        w(f"   Approved without changes: {_pct(len(as_drafted), len(reviews))} "
          f"({len(as_drafted)} of {len(reviews)} reviewed)")
    if out:
        w(f"   Closed where they were sent: {_pct(len(stayed), len(out))} "
          f"({len(stayed)} of {len(out)} closed)")
    judged = [j for j in sf if j["proposed"] == "spam" and j["outcome"] not in (None, "reject")]
    if judged:
        agree = [j for j in judged if j["approved_as"] == "spam"]
        w(f"   Spam proposals the reviewer agreed with: {_pct(len(agree), len(judged))} "
          f"({len(agree)} of {len(judged)})")
    if not (reviews or out or judged):
        w("   Nothing to measure this week.")
    return "\n".join(L)


# ---------------------------------------------------------------------------
# the run
# ---------------------------------------------------------------------------

def _email(subject: str, body: str) -> list[str]:
    from support_agent.notify import mail

    sent = []
    listed = get_settings().weekly_report_recipients or ""
    for address in [x.strip() for x in listed.split(",") if x.strip()]:
        try:
            if mail.deliver(mail.compose(address, subject, body)):
                sent.append(address)
        except Exception:
            log.exception("weekly report email failed", extra={"to": address})
    return sent


def run(key: str | None = None, *, send: bool = True, start: datetime | None = None,
        end: datetime | None = None, t2=None, journeys: bool = True) -> dict:
    """One weekly update. Claims the week first, so it cannot run twice."""
    end = end or datetime.now(UTC)
    start = start or window_start(end)
    key = key or f"manual-{end.astimezone(RIYADH):%Y%m%d-%H%M%S}"

    claimed = postgres.query_one(
        """
        INSERT INTO weekly_runs (week_key, status, window_start, window_end)
        VALUES (%s, 'running', %s, %s) ON CONFLICT (week_key) DO NOTHING RETURNING id
        """, (key, start, end))
    if claimed is None:
        return {"due": False, "reason": f"{key} already run"}
    run_id = claimed["id"]
    log.info("weekly update started", extra={"week": key, "run": run_id})

    try:
        history = add_to_history(start, end, t2=t2, journeys=journeys)
        indexes = sync_indexes(history["added"])
        g = gather(start, end, history["added"])
        report = render(start, end, history, indexes, g)
        a, b = start.astimezone(RIYADH), end.astimezone(RIYADH)
        emailed = _email(f"Support agent weekly report: {a:%d %b} to {b:%d %b %Y}",
                         report) if send else []
        summary = {"found": history["found"], "added": len(history["added"]),
                   "journeys": history["journeys"], "failed": len(history["failed"]),
                   "indexes": indexes, "reviews": len(g["reviews"]),
                   "closed_outcomes": len(g["outcomes"])}
        postgres.execute(
            "UPDATE weekly_runs SET status = 'done', summary = %s::jsonb, report = %s, "
            "emailed_to = %s, finished_at = now() WHERE id = %s",
            (json.dumps(summary, default=str), report, emailed, run_id))
        log.info("weekly update done", extra={"week": key, **summary})
        return {"due": True, "week": key, "run": run_id, **summary, "emailed": emailed,
                "report": report}
    except Exception as exc:
        err = f"{type(exc).__name__}: {exc}"[:1000]
        postgres.execute("UPDATE weekly_runs SET status = 'failed', error = %s, "
                         "finished_at = now() WHERE id = %s", (err, run_id))
        log.exception("weekly update failed", extra={"week": key})
        if send:
            _email(f"Support agent weekly update FAILED ({key})",
                   f"The weekly history update for {key} failed and the history was not "
                   f"updated.\n\n{err}\n\nIt does not retry by itself. Run it by hand with:\n"
                   f"python scripts/weekly.py run")
        return {"due": True, "week": key, "run": run_id, "error": err}
