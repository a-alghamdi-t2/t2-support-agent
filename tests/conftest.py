"""Shared test setup.

Tests that need a database run against a fresh one, created for the run from
TEST_DATABASE_URL (a postgresql:// URL whose user may create databases), with the
migrations applied. Without TEST_DATABASE_URL they are skipped; everything
else runs anywhere.
"""

from __future__ import annotations

import os
import uuid

import httpx
import pytest

import fake_t2 as fake


@pytest.fixture(autouse=True)
def no_dotenv(monkeypatch):
    """Tests never read the .env of the machine they run on: a developer's
    own settings (a password set there, say) must not change the results."""
    from support_agent import config

    monkeypatch.setitem(config.Settings.model_config, "env_file", None)
    for name in ("T2_BASE_URL", "T2_USER", "T2_PASSWORD", "GROQ_API_KEY", "SESSION_SECRET",
                 "API_KEY", "MAIL_PASSWORD", "DRY_RUN"):
        monkeypatch.delenv(name, raising=False)
    config.env_settings.cache_clear()
    config.refresh_settings()
    yield
    config.env_settings.cache_clear()
    config.refresh_settings()


@pytest.fixture(scope="session")
def database_url():
    admin = os.environ.get("TEST_DATABASE_URL")
    if not admin:
        pytest.skip("TEST_DATABASE_URL is not set")
    import psycopg
    from urllib.parse import urlsplit, urlunsplit

    name = f"agent_test_{uuid.uuid4().hex[:8]}"
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{name}"')
    url = urlunsplit(urlsplit(admin)._replace(path=f"/{name}"))
    yield url
    from support_agent.db import postgres
    postgres.close_pool()
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


@pytest.fixture
def db(database_url, monkeypatch):
    """A migrated, empty database for one test."""
    from support_agent import config
    from support_agent.db import migrate, postgres

    monkeypatch.setenv("DATABASE_URL", database_url)
    config.env_settings.cache_clear()
    config.refresh_settings()
    postgres.close_pool()
    migrate.apply()
    tables = [r["tablename"] for r in postgres.query(
        "SELECT tablename FROM pg_tables WHERE schemaname = 'public' "
        "AND tablename NOT LIKE '%yoyo%'")]
    postgres.execute("TRUNCATE " + ", ".join(tables) + " RESTART IDENTITY CASCADE")
    yield postgres
    postgres.close_pool()
    config.env_settings.cache_clear()


@pytest.fixture
def t2(monkeypatch):
    """A client talking to the fake TicketAPI."""
    from support_agent.ticketsystem import T2Client
    from support_agent.ticketsystem import client as client_module

    fake.reset()
    monkeypatch.setattr(client_module, "RETRY_WAIT", (0.0, 0.0))
    c = T2Client(base_url="http://t2.test/TicketAPI", user=fake.USER, password=fake.PASSWORD,
                 min_interval=0)
    c._http = httpx.Client(transport=fake.TRANSPORT)
    yield c
    c.close()
