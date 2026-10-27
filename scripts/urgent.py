"""Urgent keywords and who gets the alert, per product (ALL: every product),
until the AI admin pages call our API (component 10).

    python scripts/urgent.py                                  everything
    python scripts/urgent.py RICH                             what applies to RiCH
    python scripts/urgent.py add-word RICH "OTP down" --by you@t2.sa
    python scripts/urgent.py add-word ALL "متوقف" --by you@t2.sa
    python scripts/urgent.py remove-word <id> --by you@t2.sa
    python scripts/urgent.py add-person RICH oncall@t2.sa --name "RiCH on call" --by you@t2.sa
    python scripts/urgent.py remove-person <id> --by you@t2.sa
    python scripts/urgent.py emails                           the last alerts sent

Every new draft is checked; alerts go out through SMTP_HOST (DRY_RUN on:
written down, not sent).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from support_agent import options  # noqa: E402
from support_agent.db import migrate, postgres  # noqa: E402
from support_agent.notify import urgent  # noqa: E402


def _code(text: str) -> str | None:
    return None if text.upper() == "ALL" else text


def _show(code: str | None) -> None:
    words = urgent.keywords(code) if code else postgres.query(
        "SELECT * FROM urgent_keywords WHERE active ORDER BY product NULLS FIRST, keyword")
    people = urgent.recipients(code) if code else postgres.query(
        "SELECT * FROM urgent_recipients WHERE active ORDER BY product NULLS FIRST, email")
    print(f"Keywords ({len(words)}):")
    for k in words:
        print(f"  #{k['id']:<4} {k['product'] or 'ALL':<9} {k['keyword']}")
    print(f"\nWho gets the alert ({len(people)}):")
    for p in people:
        print(f"  #{p['id']:<4} {p['product'] or 'ALL':<9} {p['email']}"
              + (f"  ({p['name']})" if p["name"] else ""))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("command", nargs="?", default="")
    ap.add_argument("args", nargs="*")
    ap.add_argument("--by", default="")
    ap.add_argument("--name")
    a = ap.parse_args()
    try:
        migrate.apply()
        options.sync()
        cmd = a.command
        actions = ("add-word", "remove-word", "add-person", "remove-person", "emails")
        if cmd not in actions:                       # "", ALL or a product: show
            _show(None if cmd in ("", "list") or cmd.upper() == "ALL" else cmd)
        elif cmd == "add-word":
            urgent.add_keyword(_code(a.args[0]), " ".join(a.args[1:]), who=a.by)
            print("Added.")
        elif cmd == "remove-word":
            urgent.remove_keyword(int(a.args[0]), who=a.by)
            print("Removed.")
        elif cmd == "add-person":
            urgent.add_recipient(_code(a.args[0]), a.args[1], a.name, who=a.by)
            print("Added.")
        elif cmd == "remove-person":
            urgent.remove_recipient(int(a.args[0]), who=a.by)
            print("Removed.")
        elif cmd == "emails":
            for r in postgres.query("SELECT * FROM urgent_emails ORDER BY id DESC LIMIT 20"):
                print(f"  {r['created_at']:%Y-%m-%d %H:%M}  {r['status']:<8} {r['to_address']}"
                      f"  {r['subject']}" + (f"\n      {r['error']}" if r["error"] else ""))
        return 0
    except (ValueError, IndexError) as exc:
        print(f"Not done: {exc}")
        return 1
    finally:
        postgres.close_pool()


if __name__ == "__main__":
    raise SystemExit(main())
