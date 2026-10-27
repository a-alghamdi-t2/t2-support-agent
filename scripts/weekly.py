"""The weekly history update by hand, and a look at what it learned.

    python scripts/weekly.py                         the last runs, and what the history holds
    python scripts/weekly.py run                     run the update now, from where the last
                                                     one stopped (the first: HISTORY_FIRST_DAYS)
          [--no-email] [--no-journeys] [--days 14]   --days: only that many days back
    python scripts/weekly.py report                  the last run's report
    python scripts/weekly.py one T-2026...           add one closed ticket now, with its journey,
                                                     and show its timeline
    python scripts/weekly.py show T-2026...          a closed ticket as the history has it
    python scripts/weekly.py similar "some text"     the closed tickets most like this text
    python scripts/weekly.py spam "some text" [sender@domain]
                                                     what the spam check says about it
    python scripts/weekly.py sync                    bring both indexes in line, nothing else

Needs the ticket system (T2_*), GROQ_API_KEY for the journeys, and QDRANT_URL
for the indexes. The Friday run happens by itself with the server; a run by
hand counts as one, so the next starts where it stopped.
"""

from __future__ import annotations

import argparse
import sys
import textwrap
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from support_agent.db import migrate, postgres  # noqa: E402
from support_agent.history import index_sync, similar, spam, timeline, weekly  # noqa: E402


def _status() -> None:
    runs = postgres.query("SELECT * FROM weekly_runs ORDER BY id DESC LIMIT 8")
    print(f"Runs ({len(runs)} most recent):")
    for r in runs:
        s = r["summary"] or {}
        print(f"  {r['week_key']:<26} {r['status']:<8} "
              f"{r['window_start']:%Y-%m-%d} -> {r['window_end']:%Y-%m-%d}  "
              f"added {s.get('added', '-')}, journeys {s.get('journeys', '-')}, "
              f"problems {s.get('failed', '-')}"
              + (f"\n      {r['error']}" if r["error"] else ""))
    n = postgres.query_one(
        "SELECT count(*) FILTER (WHERE coalesce(contact_status,'') <> 'Blocked') AS real, "
        "count(*) FILTER (WHERE contact_status = 'Blocked') AS spam, "
        "(SELECT count(*) FROM ticket_journeys) AS journeys FROM closed_tickets")
    print(f"\nHistory: {n['real']} real and {n['spam']} spam closed tickets, "
          f"{n['journeys']} journeys")
    print(f"Indexes: similar {index_sync.size(similar.tenant())}, "
          f"spam {index_sync.size(spam.tenant())}")


def _show(number: str) -> int:
    c = postgres.query_one("SELECT * FROM closed_tickets WHERE ticket_number = %s",
                           (number.strip(),))
    if c is None:
        print(f"{number} is not in the history.")
        return 1
    tl = postgres.query_one("SELECT * FROM ticket_timelines WHERE closed_ticket_id = %s",
                            (c["id"],))
    j = postgres.query_one("SELECT * FROM ticket_journeys WHERE closed_ticket_id = %s",
                           (c["id"],))
    print(f"\n{c['ticket_number']}  {c['title']!r}")
    print(f"  product {c['product'] or c['product_code'] or '-'}, closed in {c['queue'] or '-'} "
          f"({c['state']}), {c['t2_closed_at']:%Y-%m-%d %H:%M}" if c["t2_closed_at"] else "")
    print(f"  sender {c['requester_email']} ({c['contact_status'] or '-'})"
          + ("  SPAM" if c["contact_status"] == "Blocked" else ""))
    if tl:
        print(f"  path {' -> '.join(tl['queue_path']) or '-'}; {tl['kept_count']} messages, "
              f"{tl['history_count']} moves, {tl['noise_count']} notes of ours left out")
        print("\n" + textwrap.indent(timeline.render(c, tl["entries"], tl["queue_path"]), "  "))
    if j:
        print(f"  JOURNEY ({j['model']}{', fallback' if j['fallback_reason'] else ''})")
        for s in j["steps"]:
            print(f"    [{s.get('article') or '-'}] {s['what']}")
        for m in j["moves"]:
            print(f"    move {m['from']} -> {m['to']}: {m['why']}")
        print("  " + textwrap.fill(j["summary"], 92, subsequent_indent="  "))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("command", nargs="?", default="status")
    ap.add_argument("args", nargs="*")
    ap.add_argument("--no-email", action="store_true")
    ap.add_argument("--no-journeys", action="store_true")
    ap.add_argument("--days", type=int)
    a = ap.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")     # Arabic on Windows
    try:
        return _run(a)
    finally:
        postgres.close_pool()


def _run(a) -> int:
    migrate.apply()
    cmd = a.command

    if cmd == "status":
        _status()
    elif cmd == "run":
        end = datetime.now(UTC)
        start = end - timedelta(days=a.days) if a.days else None
        print("Running the weekly update (closed tickets, journeys, indexes, report) ...")
        r = weekly.run(send=not a.no_email, start=start, end=end, journeys=not a.no_journeys)
        if r.get("error"):
            print(f"Failed: {r['error']}")
            return 1
        print(r.get("report") or r)
        print(f"\nEmailed to: {', '.join(r.get('emailed') or []) or 'nobody'}")
    elif cmd == "report":
        r = postgres.query_one("SELECT report FROM weekly_runs WHERE report IS NOT NULL "
                               "ORDER BY id DESC LIMIT 1")
        print(r["report"] if r else "No report yet.")
    elif cmd == "one":
        from support_agent.ticketsystem import connect

        with connect() as t2:
            tid = t2.find_number(a.args[0].strip())
            if tid is None:
                print(f"{a.args[0]} was not found in the ticket system.")
                return 1
            added = weekly.add_one(t2, tid, weekly.boilerplate_loader())
        if not added["spam"] and added["has_messages"] and not a.no_journeys:
            print("Writing its journey ...")
            weekly.journey_for(added["id"])
        return _show(added["ticket_number"])
    elif cmd == "show":
        return _show(a.args[0])
    elif cmd == "similar":
        for m in similar.find(" ".join(a.args), limit=5):
            c = postgres.query_one("SELECT title, coalesce(product, product_code) AS product, "
                                   "queue FROM closed_tickets "
                                   "WHERE id = %s", (m["id"],))
            print(f"  {m['rank']}. {m['ticket_number']}  {m['score']:.2f}  "
                  f"{c['product'] or '-'} / {c['queue'] or '-'}  {c['title']!r}")
    elif cmd == "spam":
        text, sender = a.args[0], (a.args[1] if len(a.args) > 1 else None)
        v = spam.check(text, sender)
        print(f"checked: {v.checked}, flagged: {v.flagged}"
              + (f" ({v.reason})" if v.reason else ""))
        print(f"best spam {v.score:.2f}, best real {v.real_score:.2f}; "
              f"{v.domain or '-'}: {v.sender_spam} spam, {v.sender_real} real")
        for m in v.matches:
            print(f"  {m.ticket_number}  {m.score:.2f}  {m.text[:100]!r}")
    elif cmd == "sync":
        print(weekly.sync_indexes([]))
    else:
        print(__doc__)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
