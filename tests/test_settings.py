"""The console's Settings page, with a real database."""

from __future__ import annotations

import pytest

from support_agent import settings_store
from support_agent.config import env_settings, get_settings, refresh_settings


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    monkeypatch.setenv("SESSION_SECRET", "s" * 40)
    env_settings.cache_clear()
    refresh_settings()
    yield
    env_settings.cache_clear()
    refresh_settings()


def test_save_applies_and_audits_one_row_per_setting(db):
    settings_store.save({"t2_poll_seconds": 120, "dry_run": False}, "Me@T2.sa")
    s = get_settings()
    assert s.t2_poll_seconds == 120 and s.dry_run is False
    rows = db.query("SELECT who, action, name, old_value, new_value FROM audit_log ORDER BY name")
    assert [(r["who"], r["name"], r["old_value"], r["new_value"]) for r in rows] == [
        ("me@t2.sa", "dry_run", "on", "off"),
        ("me@t2.sa", "t2_poll_seconds", "60", "120")]


def test_passwords_are_encrypted_never_shown_and_audited_without_their_value(db):
    settings_store.save({"t2_password": "very-secret-1"}, "me@t2.sa")
    assert get_settings().t2_password.get_secret_value() == "very-secret-1"
    stored = db.query_one("SELECT value, secret FROM app_settings WHERE key = 't2_password'")
    assert stored["secret"] and "very-secret-1" not in stored["value"]
    field = next(f for f in settings_store.describe()["fields"] if f["key"] == "t2_password")
    assert field["is_set"] and "value" not in field
    row = db.query_one("SELECT old_value, new_value FROM audit_log")
    assert (row["old_value"], row["new_value"]) == ("not set", "replaced")


def test_a_changed_session_secret_makes_saved_passwords_unreadable(db, monkeypatch):
    settings_store.save({"groq_api_key": "gsk-123"}, "me@t2.sa")
    monkeypatch.setenv("SESSION_SECRET", "t" * 40)
    env_settings.cache_clear()
    refresh_settings()
    assert get_settings().groq_api_key is None                       # falls back to .env
    field = next(f for f in settings_store.describe()["fields"] if f["key"] == "groq_api_key")
    assert field["unreadable"]


def test_back_to_env(db):
    settings_store.save({"t2_poll_seconds": 120}, "me@t2.sa")
    settings_store.save({"t2_poll_seconds": None}, "me@t2.sa")
    assert get_settings().t2_poll_seconds == 60
    assert db.query_one("SELECT count(*) AS n FROM app_settings")["n"] == 0


def test_bad_values_change_nothing(db):
    for change in ({"t2_poll_seconds": 5}, {"kb_tenants": "RICH"},
                   {"ticket_url": "https://x.t2.sa/Tickets/Details"}, {"similar_min_score": 2},
                   {"t2_base_url": "ticketsystem.t2.sa"}, {"api_key": "x"},
                   {"session_secret": "x"}, {"database_url": "x"}):
        with pytest.raises(settings_store.SettingsError):
            settings_store.save(change, "me@t2.sa")
    assert db.query_one("SELECT count(*) AS n FROM app_settings")["n"] == 0
    assert db.query_one("SELECT count(*) AS n FROM audit_log")["n"] == 0
