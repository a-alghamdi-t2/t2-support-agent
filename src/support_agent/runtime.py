"""Background work, inside the server process.

Four loops: one checks the intake and drafts, one writes approved drafts to
the ticket system, one indexes uploaded knowledge base documents, one runs the
Friday history update. All run on a worker thread -- deciding a ticket is a
minute of model time and must not touch the event loop -- and all survive
their own failures, because a knowledge base that is down for ten minutes
should not stop the console.

All of them are always started, and each pass reads its switch first
(POLLER_ENABLED, WRITER_ENABLED, WEEKLY_ENABLED, which the Settings page can
change): turned off, a pass does nothing; turned back on, the next pass works.
No restart either way.

None of them is safe to run twice. One poll deciding a ticket while another
decides the same one wastes model time and races on the one-pending-draft
index. That is why the server runs a single worker, and why --reload is for
development only.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Callable

from starlette.concurrency import run_in_threadpool

from support_agent.config import get_settings
from support_agent.logging import get_logger

log = get_logger(__name__)


@dataclass
class LoopState:
    """What a loop has been doing, for /api/health."""

    name: str
    enabled: bool = False
    interval: float = 0.0
    runs: int = 0
    last_run_at: datetime | None = None
    last_error: str | None = None
    last_result: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "interval_seconds": self.interval,
            "runs": self.runs,
            "last_run_at": self.last_run_at.isoformat() if self.last_run_at else None,
            "last_error": self.last_error,
            "last_result": self.last_result,
        }


STATE: dict[str, LoopState] = {}
_TASKS: list[asyncio.Task] = []


async def _loop(state: LoopState, work: Callable[[], dict[str, Any]]) -> None:
    """Run `work` on a thread every `state.interval` seconds, forever.

    The interval is measured between finishing and starting again, not between
    starts: a pass that took four minutes should not immediately trigger the
    four that were due while it ran.
    """
    log.info("loop started", extra={"loop": state.name, "every": state.interval})
    try:
        while True:
            started = datetime.now(UTC)
            try:
                result = await run_in_threadpool(work)
                if result.get("off"):        # switched off: nothing ran
                    await asyncio.sleep(state.interval)
                    continue
                state.last_error = None
                state.last_result = result
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                state.last_error = f"{type(exc).__name__}: {exc}"[:500]
                log.exception("loop failed", extra={"loop": state.name})
            state.runs += 1
            state.last_run_at = started
            await asyncio.sleep(state.interval)
    except asyncio.CancelledError:
        log.info("loop stopped", extra={"loop": state.name})
        raise


def _switch(name: str, on: bool, interval: float | None = None,
            why: str = "switched off") -> dict[str, Any] | None:
    """Read before each pass: whether the loop is on, and how often it runs."""
    state = STATE[name]
    if interval:
        state.interval = interval
    if on != state.enabled:
        log.info("loop " + ("on" if on else "off"), extra={"loop": name})
    state.enabled = on
    return None if on else {"off": why}


def _poll_once() -> dict[str, Any]:
    from support_agent.intake.poller import poll_once

    s = get_settings()
    off = _switch("poller", s.poller_enabled and s.tickets_configured,
                  float(s.t2_poll_seconds),
                  "switched off" if not s.poller_enabled
                  else "the ticket system is not configured")
    if off:
        return off
    result = poll_once()
    return {"seen": result.seen, "drafted": result.drafted,
            "skipped": result.skipped, "failed": result.failed,
            "seconds": round(result.seconds, 1)}


def _write_once() -> dict[str, Any]:
    from support_agent.review import writer

    off = _switch("writer", get_settings().writer_enabled)
    if off:
        return off
    sent, failed = writer.send_pending()
    return {"sent": sent, "failed": failed}


INDEX_SECONDS = 30.0


def _index_once() -> dict[str, Any]:
    from support_agent.knowledge import documents

    s = get_settings()
    off = _switch("indexer", bool(s.qdrant_url), why="the knowledge base is not configured")
    if off:
        return off
    indexed, failed = documents.index_pending()
    return {"indexed": indexed, "failed": failed}


def _weekly_once() -> dict[str, Any]:
    from support_agent.history import weekly

    off = _switch("weekly", get_settings().weekly_enabled)
    if off:
        return off
    return weekly.maybe_run()


# How often the weekly loop looks at the clock. The run itself happens once a
# week; checking every five minutes means it starts within five of 04:00.
WEEKLY_CHECK_SECONDS = 300.0


def start() -> None:
    """Start the loops. Called once, from the lifespan. Each pass checks its
    own switch, so a loop that is off now can be turned on later."""
    s = get_settings()

    poller = LoopState("poller", interval=float(s.t2_poll_seconds))
    writer = LoopState("writer", interval=float(s.writer_seconds))
    # The weekly history update: Friday 04:00 Riyadh time. The loop only looks
    # at the clock; the week's row in weekly_runs keeps it to one run a week.
    weekly = LoopState("weekly", interval=WEEKLY_CHECK_SECONDS)
    STATE["poller"] = poller
    STATE["writer"] = writer
    STATE["weekly"] = weekly
    indexer = LoopState("indexer", interval=INDEX_SECONDS)
    STATE["indexer"] = indexer
    indexer.enabled = bool(s.qdrant_url)
    poller.enabled = s.poller_enabled and s.tickets_configured
    writer.enabled = s.writer_enabled
    weekly.enabled = s.weekly_enabled
    if s.poller_enabled and not s.tickets_configured:
        log.warning("poller waiting: the ticket system is not configured")

    for state, work in ((poller, _poll_once), (writer, _write_once), (indexer, _index_once),
                        (weekly, _weekly_once)):
        _TASKS.append(asyncio.create_task(_loop(state, work), name=state.name))


async def stop() -> None:
    """Cancel the loops and wait for them. A pass in flight is allowed to be
    interrupted: a draft half-decided is simply not saved, and the next pass
    picks the ticket up again."""
    for task in _TASKS:
        task.cancel()
    if _TASKS:
        await asyncio.gather(*_TASKS, return_exceptions=True)
    _TASKS.clear()


def status() -> dict[str, Any]:
    return {name: state.as_dict() for name, state in STATE.items()}
