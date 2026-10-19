"""Check the connection to the T2 Ticketing System, using the agent's own client.

    python scripts/check_t2.py                 read only: credentials, values, intake
    python scripts/check_t2.py --write         also routes the oldest intake ticket
                                               (local copies only)

Reads T2_BASE_URL, T2_USER and T2_PASSWORD from .env. --write refuses to run
against anything but a copy on this machine.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from support_agent.config import env_settings  # noqa: E402
from support_agent.ticketsystem import Note, T2Client, T2Error  # noqa: E402

LOCAL_HOSTS = ("localhost", "127.0.0.1", "host.docker.internal")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true",
                    help="route the oldest intake ticket (local copies only)")
    args = ap.parse_args()

    s = env_settings()
    if not s.tickets_configured:
        print("Set T2_BASE_URL, T2_USER and T2_PASSWORD in .env first.")
        return 2
    print(f"Ticket system: {s.t2_base_url}")
    with T2Client.from_settings(s) as t2:
        try:
            t2.ping()
        except T2Error as e:
            print(f"  FAILED: {e}")
            return 1
        print("  connected, credentials accepted")

        lk = t2.lookups()
        print(f"\nValues: {len(lk.products)} products, {len(lk.statuses)} statuses, "
              f"{len(lk.priorities)} priorities")
        for p in lk.products:
            subtypes = sum(len(v) for v in p.types.values())
            linked = sum(1 for v in p.types.values() for x in v if x.reason)
            print(f"  {p.code:<9} {p.name:<20} {len(p.queues)} queues, {len(p.types)} types, "
                  f"{subtypes} subtypes ({linked} with their reason), {len(p.reasons)} reasons")

        ids = t2.intake()
        print(f"\nIntake: {len(ids)} email ticket(s) waiting")
        if not ids:
            return 0
        t = t2.get(ids[0])
        print(f"  oldest: {t.number}  {t.title!r}")
        print(f"    from {t.customer_email} ({t.customer_status}), created {t.created}")
        print(f"    product {t.product} ({t.product_code}), queue {t.queue or '-'}, "
              f"{len(t.articles)} article(s), {len(t.history)} history entr(ies)")
        if t.first_article:
            print(f"    first message: {t.first_article.body[:120]!r}")

        if not args.write:
            return 0
        host = urlparse(s.t2_base_url).hostname or ""
        if host not in LOCAL_HOSTS:
            print(f"\n--write refused: {host} is not a copy on this machine.")
            return 2
        rich = lk.product("RICH")
        if not rich or not rich.queues or not rich.types:
            print("\n--write: RiCH has no queues or types here; nothing to route to.")
            return 2
        type_name, subtypes = next((k, v) for k, v in rich.types.items() if v)
        sub = subtypes[0]
        print(f"\nRouting {t.number} to {rich.name} / {rich.queues[0]} / {type_name} / {sub.name}")
        r = t2.update(t.id, product=rich.name, queue=rich.queues[0], type=type_name,
                      subtype=sub.name, conclusions=sub.reason,
                      note=Note("Support Agent: connection check",
                                "Written by scripts/check_t2.py to test the connection."))
        after = t2.get(t.id)
        print(f"  done: note {r.article_id}")
        print(f"  now: product {after.product}, queue {after.queue}, type {after.type}, "
              f"subtype {after.subtype}, waiting for classification: "
              f"{after.requires_classification}")
        for h in after.history:
            print(f"    {h.at:%H:%M:%S}  {h.action:<20} {h.details[:70]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
