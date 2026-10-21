"""Bring the agent's values in line with the ticket system, and show them.

    python scripts/values.py            sync now, then list what the agent may choose
    python scripts/values.py --new      list only the values marked New

Needs DATABASE_URL and the T2_* settings in .env. Applies migrations first.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from support_agent import options  # noqa: E402
from support_agent.db import migrate, postgres  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--new", action="store_true", help="only the values marked New")
    args = ap.parse_args()
    try:
        migrate.apply()
        r = options.sync(force=True)
        if r.skipped:
            print(f"Not refreshed: {r.skipped}")
        else:
            print(f"Refreshed: {r.added} added, {r.removed} removed, {r.returned} back, "
                  f"{r.updated} updated, {r.defaults} default(s) chosen")
        print(f"New, not looked at yet: {options.new_count()}\n")

        rows = options.everything()
        if args.new:
            for o in rows:
                if o.is_new:
                    where = " / ".join(x for x in (o.product, o.parent) if x)
                    print(f"  {o.kind:<10} {where:<30} {o.value}")
            return 0

        for p in options.products():
            code = p.product
            types = options.types(code)
            print(f"{p.value} ({code})")
            print(f"  queues: {', '.join(q.value for q in options.queues(code)) or '-'}")
            print(f"  types:  {len(types)}, subtypes offered: "
                  f"{sum(len(t.subtypes) for t in types)}")
            dq, dt = options.default("queue", code), options.default("type", code)
            print(f"  defaults: queue {dq.value if dq else '-'}, type {dt.value if dt else '-'}")
        hidden = [o for o in rows if o.hidden]
        removed = [o for o in rows if o.removed]
        print(f"\nStatuses:   {', '.join(o.value for o in options.statuses())}")
        print(f"Priorities: {', '.join(o.value for o in options.priorities())}")
        ds, dp = options.default("next_state"), options.default("priority")
        print(f"Defaults:   status {ds.value if ds else '-'}, priority {dp.value if dp else '-'}")
        print(f"Hidden from the agent: {', '.join(o.value for o in hidden) or '-'}")
        print(f"Removed from the ticket system: {len(removed)}")
        return 0
    finally:
        postgres.close_pool()


if __name__ == "__main__":
    sys.exit(main())
