"""Review drafts and write them to the ticket, by hand, until their screens
call our API (component 10). What the panel's buttons will do.

    python scripts/review.py                                  drafts waiting, and writes
                                                              that need a look
    python scripts/review.py approve T-2026... --by you@t2.sa [--send]
          [--set queue="RiCH Billing" --set type=Inquiry ...] [--comment "..."]
                                                              Approve & Send (--send),
                                                              Approve & Draft, Edit (--set)
    python scripts/review.py reject T-2026... --by you@t2.sa --reason "..."
    python scripts/review.py redraft T-2026... --by you@t2.sa --product OLE5
    python scripts/review.py write                            write what is approved
    python scripts/review.py send-again <write id> --by you@t2.sa
    python scripts/review.py settle <write id> --by you@t2.sa --happened yes|no
    python scripts/review.py signature [--product RICH] --by you@t2.sa
          --arabic "..." --english "..."                      no --product: every product

`write` needs DRY_RUN=false in .env and only runs against a local copy of
the ticket system.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from support_agent.config import get_settings  # noqa: E402
from support_agent.db import migrate, postgres  # noqa: E402
from support_agent.review import actions, signatures, writer  # noqa: E402


def _draft_of(number: str) -> int:
    row = postgres.query_one(
        "SELECT d.id FROM drafts d JOIN tickets t ON t.id = d.ticket_id "
        "WHERE t.ticket_number = %s AND d.status = 'pending'", (number.strip(),))
    if row is None:
        raise actions.ReviewError(f"{number} has no draft waiting for review")
    return row["id"]


def _list() -> None:
    rows = postgres.query(
        """
        SELECT t.ticket_number, t.title, t.customer_contact_status AS contact, d.action,
               d.reply_kind, d.product, d.product_sure, d.queue, d.type, d.subtype
        FROM drafts d JOIN tickets t ON t.id = d.ticket_id
        WHERE d.status = 'pending' ORDER BY d.created_at""")
    print(f"Waiting for review: {len(rows)}")
    for r in rows:
        what = r["action"] + (" (asks for more)" if r["reply_kind"] == "ask_more" else "")
        sure = "" if r["product_sure"] else " NOT SURE"
        print(f"  {r['ticket_number']}  {r['title'][:40]!r}")
        if r["action"] == "spam":
            print(f"      spam   sender {r['contact'] or '-'}")
        else:
            print(f"      {what}: {r['product'] or '-'}{sure} / {r['queue'] or '-'} / "
                  f"{r['type'] or '-'} / {r['subtype'] or '-'}   sender {r['contact'] or '-'}")
    writes = postgres.query(
        """
        SELECT w.id, w.kind, w.status, w.attempts, w.last_error, t.ticket_number
        FROM ticket_writes w JOIN drafts d ON d.id = w.draft_id
        JOIN tickets t ON t.id = d.ticket_id
        WHERE w.status <> 'sent' ORDER BY w.created_at""")
    print(f"\nWrites not done: {len(writes)}")
    for w in writes:
        print(f"  #{w['id']} {w['ticket_number']} {w['kind']}: {w['status']}"
              + (f" ({w['attempts']} tries)" if w["attempts"] else "")
              + (f"\n      {w['last_error']}" if w["last_error"] else ""))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("command", nargs="?", default="list",
                    choices=["list", "approve", "reject", "redraft", "write", "send-again",
                             "settle", "signature"])
    ap.add_argument("target", nargs="?", help="a ticket number, or a write id")
    ap.add_argument("--by", help="the reviewer's email")
    ap.add_argument("--send", action="store_true", help="Approve & Send")
    ap.add_argument("--set", action="append", default=[], metavar="FIELD=VALUE")
    ap.add_argument("--comment")
    ap.add_argument("--reason")
    ap.add_argument("--product")
    ap.add_argument("--happened", choices=["yes", "no"])
    ap.add_argument("--arabic")
    ap.add_argument("--english")
    args = ap.parse_args()

    if args.command not in ("list", "write") and not args.by:
        print("Say who you are: --by you@t2.sa")
        return 1
    try:
        migrate.apply()
        if args.command == "list":
            _list()
        elif args.command == "write":
            s = get_settings()
            host = urlsplit(s.t2_base_url or "").hostname or ""
            if s.dry_run:
                print("DRY_RUN is on: nothing would be written. Set DRY_RUN=false in .env.")
                return 1
            if host not in ("localhost", "127.0.0.1"):
                print(f"Refused: {host} is not a local copy of the ticket system.")
                return 1
            sent, other = writer.send_pending()
            print(f"Written: {sent}, not written: {other}")
            _list()
        elif args.command == "signature":
            signatures.save(args.product, arabic=args.arabic, english=args.english,
                            who=args.by or "")
            print(f"Saved for {args.product or 'every product'}.")
        else:
            if not args.target:
                print("Which ticket (or write)?")
                return 1
            if args.command == "approve":
                changes = {}
                for pair in args.set:
                    k, _, v = pair.partition("=")
                    changes[k.strip()] = v
                a = actions.approve(_draft_of(args.target), who=args.by or "", send=args.send,
                                    changes=changes, comment=args.comment)
                print(f"{a.outcome}: will write {', '.join(a.writes) or 'nothing'}"
                      + (f"; changed {', '.join(a.changed_fields)}" if a.changed_fields else ""))
            elif args.command == "reject":
                actions.reject(_draft_of(args.target), who=args.by or "", reason=args.reason or "")
                print("Rejected.")
            elif args.command == "redraft":
                new = actions.redraft(_draft_of(args.target), who=args.by or "",
                                      product=args.product or "")
                print(f"Drafted again: draft {new}. See it with "
                      f"scripts\\draft.py {args.target} --show")
            elif args.command == "send-again":
                actions.send_again(int(args.target), who=args.by or "")
                print("It will be sent on the next write.")
            elif args.command == "settle":
                actions.settle(int(args.target), who=args.by or "",
                               happened=args.happened == "yes")
                print("Settled.")
        return 0
    except ValueError as exc:            # ReviewError, SignatureError: for the reviewer
        print(f"Not done: {exc}")
        return 1
    finally:
        postgres.close_pool()


if __name__ == "__main__":
    raise SystemExit(main())
