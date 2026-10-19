"""Manages a lazily-opened connection pool (one per process) and centralizes queries through query or execute. 
   This prevents modules from building their own connections and ensures slow statements are logged in one place.
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from typing import Any, Iterator, Sequence

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from support_agent.config import env_settings
from support_agent.logging import get_logger

log = get_logger(__name__)

_pool: ConnectionPool | None = None

SLOW_QUERY_MS = 1000


def get_pool() -> ConnectionPool:
    """Initializes the pool on first use. It uses generous timeouts because Neon suspends idle computes, 
       causing the first connection after a pause to be slow."""
    global _pool
    if _pool is None:
        # .env alone: the Settings page's values are read through this pool.
        settings = env_settings()
        _pool = ConnectionPool(
            conninfo=settings.db_dsn,
            min_size=1,
            max_size=settings.db_pool_max,
            timeout=settings.db_connect_timeout,
            kwargs={"connect_timeout": settings.db_connect_timeout,
                    "row_factory": dict_row},
            open=True,
        )
        log.debug("pool opened", extra={"max_size": settings.db_pool_max})
    return _pool


def close_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


@contextmanager
def connection() -> Iterator[Any]:
    """Yields a pooled, transactional connection that automatically commits on exit or rolls back on exceptions. 
       It should only be used directly when multiple statements require atomicity"""
    with get_pool().connection() as conn:
        yield conn


@contextmanager
def cursor() -> Iterator[Any]:
    with connection() as conn:
        with conn.cursor() as cur:
            yield cur


def _timed(cur: Any, sql: str, params: Sequence[Any] | None) -> None:
    started = time.perf_counter()
    cur.execute(sql, params)
    elapsed = (time.perf_counter() - started) * 1000
    if elapsed > SLOW_QUERY_MS:
        log.warning("slow query", extra={"ms": round(elapsed), "sql": " ".join(sql.split())[:120]})


def query(sql: str, params: Sequence[Any] | None = None) -> list[dict]:
    """Returns matching rows as a list of dictionaries, or an empty list if nothing matched."""
    with cursor() as cur:
        _timed(cur, sql, params)
        return cur.fetchall()


def query_one(sql: str, params: Sequence[Any] | None = None) -> dict | None:
    with cursor() as cur:
        _timed(cur, sql, params)
        return cur.fetchone()


def execute(sql: str, params: Sequence[Any] | None = None) -> int:
    """Returns the number of rows affected by the query. 
       Advises using query_one instead for INSERT ... RETURNING operations."""
    with cursor() as cur:
        _timed(cur, sql, params)
        return cur.rowcount


def ping() -> bool:
    try:
        return query_one("SELECT 1 AS ok") == {"ok": 1}
    except Exception:
        log.exception("database ping failed")
        return False