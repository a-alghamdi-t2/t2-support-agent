"""Applies database migrations internally during startup. 
   Uses yoyo advisory locks to ensure safety when multiple containers start simultaneously (one runs, others wait), 
   which requires using a direct DSN instead of a pooler to maintain a real session.
"""

from __future__ import annotations

from support_agent.config import env_settings
from support_agent.logging import get_logger

log = get_logger(__name__)


def apply() -> int:
    """Applies all outstanding migrations and returns the total number of migrations executed."""
    from yoyo import get_backend, read_migrations

    s = env_settings()
    migrations = read_migrations(str(s.migrations_dir))
    if not migrations:
        log.warning("no migrations found", extra={"dir": str(s.migrations_dir)})
        return 0

    backend = get_backend(s.db_dsn_yoyo)
    with backend.lock():
        pending = backend.to_apply(migrations)
        if not pending:
            log.info("schema up to date", extra={"known": len(migrations)})
            return 0
        log.info("applying migrations", extra={"count": len(pending)})
        backend.apply_migrations(pending)
    return len(pending)