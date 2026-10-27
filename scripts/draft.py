"""Make drafts and show them. Nothing is written to the ticket system.

    python scripts/draft.py                       check the intake, draft what is waiting
    python scripts/draft.py T-2026...             draft one ticket again
    python scripts/draft.py T-2026... --product OLE5
                                                  draft it again for that product, as
                                                  "Draft again for this product" will
    python scripts/draft.py T-2026... --show      show its latest draft, step by step

Needs DATABASE_URL, the T2_* settings and GROQ_API_KEY in .env.
"""

from __future__ import annotations

import argparse
import json
import sys
import textwrap
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from support_agent import options  # noqa: E402
from support_agent.config import get_settings  # noqa: E402
from support_agent.db import migrate, postgres  # noqa: E402


def _name(code: str | None) -> str:
    o = options.product(code) if code else None
    return f"{o.value} ({o.product})" if o else (code or "-")


def show(draft_id: int, *, steps: bool = False) -> None:
    d = postgres.query_one(
        "SELECT d.*, t.ticket_number, t.title FROM drafts d JOIN tickets t ON t.id = d.ticket_id "
        "WHERE d.id = %s", (draft_id,))
    tokens = (d["trace"][-1].get("tokens") or {}) if d["trace"] else {}
    total = sum(tokens.values())
    print(f"\n{d['ticket_number']}  {d['title']!r}  "
          f"(draft {d['id']}, {d['latency_ms'] / 1000:.1f} s, {total:,} tokens)")
    if d["action"] == "spam":
        print("  product      - (spam)")
    else:
        sure = "sure" if d["product_sure"] else "NOT SURE"
        print(f"  product      {_name(d['product'])}  {sure}")
    # Step 1's reason and the other possible product are kept in the trace.
    step1 = next((t for t in reversed(d["trace"] or []) if t.get("step") == "product"), None)
    if step1 and d["action"] != "spam":
        proposed = step1.get("proposed") or {}
        why = proposed.get("reason") or "-"
        if proposed.get("other"):
            why += f"  (could be {proposed['other']})"
        print(f"  product why  {why}")
    action = d["action"] + (" (asks for more)" if d["reply_kind"] == "ask_more" else "")
    print(f"  action       {action}")
    if d["action"] != "spam":
        print(f"  queue        {d['queue'] or '-'}" +
              (f"  ({d['queue_reason']})" if d["queue_reason"] else ""))
        print(f"  type         {d['type'] or '-'} / {d['subtype'] or '-'}")
        print(f"  conclusions  {d['conclusions'] or '-'}")
        print(f"  status       {d['next_state'] or '-'}    priority {d['priority'] or '-'}"
              f"    confidence {d['confidence'] or '-'}")
    print(f"  summary      {d['summary']}")
    if d["issue_type"]:
        print(f"  issue type   {d['issue_type']}")
    for k, v in (d["collected"] or {}).items():
        print(f"  collected    {k}: {v}")
    for m in d["missing"] or []:
        print(f"  missing      {m}")
    if d["reasoning"]:
        print("  reasoning    " + textwrap.fill(d["reasoning"], 90,
                                                subsequent_indent=" " * 15))
    if d["note"]:
        print(f"  note         {d['note']}")
    ev = d["evidence"] or {}
    for s in ev.get("searches", []):
        found = f"{len(s['evidence'])} passage(s)" + (f", {s['error']}" if s["error"] else "")
        secs = f", {s['elapsed_ms'] / 1000:.0f} s" if s.get("elapsed_ms") else ""
        print(f"  searched     {s['query']!r} -> {s['verdict']} ({found}{secs})")
        # Each passage it found: which file, which section, and with --show
        # the passage itself, so a reply can be checked against its source.
        for e in s["evidence"]:
            where = " / ".join(x for x in (e.get("source_file") or e.get("document"),
                                           e.get("section")) if x)
            print(f"    [{e['relation']}] {where}")
            if steps:
                if e.get("reason"):
                    print("      why: " + textwrap.shorten(e["reason"], 200))
                for line in (e.get("text") or "").splitlines():
                    print(f"      | {line}")
        for c in s.get("considered") or []:
            print(f"    considered, not used: {c}")
    for c in ev.get("corrections", []):
        print(f"  corrected    {c}")
    urgent = postgres.query_one("SELECT by_agent, agent_reason, keywords FROM draft_urgent "
                                "WHERE draft_id = %s", (draft_id,))
    if urgent:
        if urgent["keywords"]:
            print(f"  URGENT       keyword: {', '.join(urgent['keywords'])}")
        if urgent["by_agent"]:
            print(f"  URGENT       agent: {urgent['agent_reason'] or 'judged it urgent'}")
        for e in postgres.query("SELECT to_address, status FROM urgent_emails "
                                "WHERE draft_id = %s ORDER BY id", (draft_id,)):
            print(f"  alerted      {e['to_address']}: {e['status']}")
    reply = d["reply_body"] or ((ev.get("held_reply") or {}).get("body"))
    if reply:
        label = "reply" if d["reply_body"] else (
            f"reply kept for the reviewer, not sent ({ev['held_reply'].get('why')})")
        print(f"  {label}:")
        print(textwrap.indent(reply.strip(), "    "))
    if steps:
        print("\n  steps:")
        for t in d["trace"]:
            print("    " + json.dumps(t, ensure_ascii=False, default=str)[:400])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("ticket", nargs="?", help="a ticket number, e.g. T-20261019051928042-0002")
    ap.add_argument("--product", help="draft again for this product code")
    ap.add_argument("--show", action="store_true", help="show the latest draft, with its steps")
    args = ap.parse_args()

    if not get_settings().groq_api_key and not args.show:
        print("GROQ_API_KEY is not set in .env")
        return 1
    try:
        migrate.apply()
        from support_agent.intake import poller
        from support_agent.orchestrator import pipeline

        if not args.ticket:
            r = poller.poll_once()
            print(f"Intake: {r.seen} waiting, {r.drafted} drafted now, {r.skipped} drafted "
                  f"before, {r.failed} failed, {r.seconds:.0f} s")
            # The latest draft of every waiting ticket, new or from an earlier run.
            for row in postgres.query(
                    "SELECT DISTINCT ON (d.ticket_id) d.id FROM drafts d "
                    "JOIN tickets t ON t.id = d.ticket_id WHERE t.ticket_number = ANY(%s) "
                    "ORDER BY d.ticket_id, d.id DESC", (r.tickets,)):
                show(row["id"], steps=args.show)
            return 0 if not r.failed else 1

        t = postgres.query_one("SELECT id FROM tickets WHERE ticket_number = %s",
                               (args.ticket.strip(),))
        if t is None:
            print(f"{args.ticket} is not stored here. Run without a ticket first.")
            return 1
        if args.show:
            d = postgres.query_one("SELECT id FROM drafts WHERE ticket_id = %s "
                                   "ORDER BY id DESC LIMIT 1", (t["id"],))
            if d is None:
                print("No draft yet.")
                return 1
            show(d["id"], steps=True)
            return 0

        replaces = None
        if args.product:
            # As the review panel will do: the pending draft is rejected
            # first, and the new one points at it.
            old = postgres.query_one(
                "UPDATE drafts SET status = 'rejected' WHERE ticket_id = %s "
                "AND status = 'pending' RETURNING id", (t["id"],))
            replaces = old["id"] if old else None
        try:
            draft_id = pipeline.draft(t["id"], product=args.product, replaces=replaces)
        except pipeline.DecisionFailed as exc:
            print(f"No draft: {exc}")
            return 1
        show(draft_id)
        return 0
    finally:
        postgres.close_pool()


if __name__ == "__main__":
    raise SystemExit(main())
