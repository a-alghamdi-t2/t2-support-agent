"""Logging. One configuration, applied once, at process start.

Console format for a person reading a terminal, JSON for anything shipping logs
somewhere. Nothing else in the codebase calls logging.basicConfig.
"""

from __future__ import annotations

import json
import logging
import sys
from typing import Any

_configured = False

_RESERVED = {
    "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
    "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
    "created", "msecs", "relativeCreated", "thread", "threadName",
    "processName", "process", "taskName", "message", "asctime",
}


def _extras(record: logging.LogRecord) -> dict[str, Any]:
    return {k: v for k, v in record.__dict__.items() if k not in _RESERVED}


class ConsoleFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        base = super().format(record)
        extras = _extras(record)
        if extras:
            base += "  " + " ".join(f"{k}={v}" for k, v in extras.items())
        return base


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        payload.update(_extras(record))
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def setup_logging() -> None:
    """Idempotent: safe to call from every entry point."""
    global _configured
    if _configured:
        return

    from support_agent.config import get_settings

    settings = get_settings()

    handler = logging.StreamHandler(sys.stdout)
    if settings.log_format == "json":
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(
            ConsoleFormatter("%(asctime)s %(levelname)-7s %(name)-22s %(message)s",
                             datefmt="%H:%M:%S")
        )

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(settings.log_level.upper())

    # These are noisy at INFO and say nothing we need.
    for noisy in ("httpx", "httpcore", "urllib3", "psycopg.pool"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    _configured = True


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)