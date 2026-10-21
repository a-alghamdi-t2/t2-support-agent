"""A local mail catcher, for testing Approve & Send against a local copy of
the ticket system. Nothing is sent anywhere: every email their system sends
to localhost:1025 is saved as an .html file you can open in a browser, and
its sender, recipient and subject are printed.

    python scripts/mailcatcher.py              listens on localhost:1025, saves to mail/

Their system must have Smtp:Host=localhost, Smtp:Port=1025, Smtp:EnableSsl=false
in its user-secrets, and no Gmail connected.
"""

from __future__ import annotations

import re
import sys
import time
from datetime import datetime
from email import message_from_bytes, policy
from pathlib import Path

from aiosmtpd.controller import Controller

OUT = Path(__file__).resolve().parents[1] / "mail"


class Save:
    async def handle_DATA(self, server, session, envelope):
        msg = message_from_bytes(envelope.content, policy=policy.default)
        body = msg.get_body(preferencelist=("html", "plain"))
        html = body.get_content() if body else ""
        subject = str(msg["Subject"] or "(no subject)")
        name = re.sub(r"[^\w\-]+", "_", subject)[:60]
        OUT.mkdir(exist_ok=True)
        path = OUT / f"{datetime.now():%H%M%S}-{name}.html"
        path.write_text(html, encoding="utf-8")
        print(f"{datetime.now():%H:%M:%S}  to {', '.join(envelope.rcpt_tos)}")
        print(f"          from {msg['From']}")
        print(f"          subject {subject}")
        print(f"          saved {path}\n", flush=True)
        return "250 OK"


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")     # Arabic subjects on Windows
    controller = Controller(Save(), hostname="localhost", port=1025)
    controller.start()
    print(f"Catching email on localhost:1025, saving to {OUT}. Ctrl+C to stop.\n", flush=True)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        controller.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
