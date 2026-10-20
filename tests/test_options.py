"""The values sync against the fake TicketAPI and a real database."""

from __future__ import annotations

import pytest

import fake_t2 as fake
from support_agent import options


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    monkeypatch.setattr(options, "_last_sync", float("-inf"))
    options.invalidate()
    yield
    options.invalidate()


def _count(db, where="true"):
    return db.query_one(f"SELECT count(*) AS n FROM agent_options WHERE {where}")["n"]


def test_first_sync_copies_every_value(db, t2):
    r = options.sync(t2=t2)
    assert r.added == _count(db) and r.removed == 0
    assert {o.value for o in options.products()} == {"RiCH", "Ole5"}    # intake product hidden
    assert options.product("EMAIL").hidden
    assert [o.value for o in options.queues("RICH")] == ["RiCH Support", "RiCH Billing",
                                                          "RiCH Technical"]   # their order
    assert {o.value: o.parent for o in options.statuses()}["closed successful"] == "closed"
    assert [o.value for o in options.priorities()] == ["Low", "Medium", "High", "Critical"]
    assert [o.value for o in options.statuses()][:2] == ["New", "Open"]   # their order
    assert options.new_count() == _count(db, "NOT hidden")


def test_sync_is_skipped_while_fresh_and_quiet_when_nothing_changed(db, t2):
    options.sync(t2=t2)
    calls = len(fake.calls)
    assert options.sync(t2=t2).skipped == "fresh" and len(fake.calls) == calls
    again = options.sync(t2=t2, force=True)
    assert (again.added, again.removed, again.returned, again.updated) == (0, 0, 0, 0)


def test_new_removed_and_returning_values(db, t2):
    options.sync(t2=t2)
    queue = options.find("queue", "RiCH Billing", product="RICH")
    options.describe(queue.id, "Invoices, payments and refunds.", who="admin@t2.sa")

    fake.PRODUCTS["RICH"]["queues"].remove("RiCH Billing")
    fake.PRODUCTS["RICH"]["queues"].append("RiCH Escalations")
    try:
        r = options.sync(t2=t2, force=True)
        assert (r.added, r.removed) == (1, 1)
        assert options.find("queue", "RiCH Billing", product="RICH") is None   # not offered
        new = options.find("queue", "RiCH Escalations", product="RICH")
        assert new.is_new and new.description is None
    finally:
        fake.PRODUCTS["RICH"]["queues"].remove("RiCH Escalations")
        fake.PRODUCTS["RICH"]["queues"].insert(1, "RiCH Billing")

    r = options.sync(t2=t2, force=True)
    assert (r.returned, r.removed) == (1, 1)
    back = options.find("queue", "RiCH Billing", product="RICH")
    assert back.description == "Invoices, payments and refunds."    # kept while it was gone


def test_subtype_reasons_follow_the_ticket_system(db, t2):
    options.sync(t2=t2)
    # No reasons in the catalog yet: every subtype is offered.
    inquiry = next(t for t in options.types("RICH") if t.option.value == "Inquiry")
    assert [s.value for s in inquiry.subtypes] == ["DLR - Call Barring", "User Permissions"]
    assert inquiry.subtypes[0].conclusion is None

    fake.subtypes_with_reasons = True
    fake.REASONS.pop("User Permissions")
    try:
        r = options.sync(t2=t2, force=True)
        assert r.updated >= 1
        inquiry = next(t for t in options.types("RICH") if t.option.value == "Inquiry")
        # Reasons now exist for RiCH: the one without a reason is no longer offered.
        assert [s.value for s in inquiry.subtypes] == ["DLR - Call Barring"]
        assert inquiry.subtypes[0].conclusion.startswith("Issue is between")
    finally:
        fake.REASONS["User Permissions"] = "A user reports they cannot perform an action."


def test_default_type_is_hidden_on_arrival(db, t2):
    fake.PRODUCTS["OLE5"]["types"]["Default"] = []
    try:
        options.sync(t2=t2)
        assert "Default" not in [t.option.value for t in options.types("OLE5")]
        assert options.find("type", "Default", product="OLE5") is None
    finally:
        del fake.PRODUCTS["OLE5"]["types"]["Default"]


def test_a_product_added_later_waits_for_a_description(db, t2):
    options.sync(t2=t2)
    fake.PRODUCTS["TQN"] = {"name": "Taqnia", "queues": ["Taqnia Support"],
                            "types": {"Inquiry": ["General"]}}
    try:
        options.sync(t2=t2, force=True)
        tqn = options.product("TQN")
        assert tqn.hidden and tqn.is_new                     # New on the admin page
        assert "Taqnia" not in [o.value for o in options.products()]    # step 1 can't pick it
        assert [o.value for o in options.queues("TQN")] == ["Taqnia Support"]   # ready below
        options.mark_seen(tqn.id)
        assert options.product("TQN").hidden                 # opening it is not enough
        options.describe(tqn.id, "Taqnia cloud services: VMs, storage, backups.",
                         who="ai.admin@t2.sa")
        assert "Taqnia" in [o.value for o in options.products()]
        assert not options.product("TQN").is_new
        # Hidden on purpose later: describing again does not bring it back.
        options.set_hidden(tqn.id, True, who="ai.admin@t2.sa")
        options.describe(tqn.id, "Taqnia cloud.", who="ai.admin@t2.sa")
        assert options.product("TQN").hidden
    finally:
        del fake.PRODUCTS["TQN"]


def test_changes_by_people_are_audited_and_clear_new(db, t2):
    options.sync(t2=t2)
    q = options.find("queue", "Ole5 Technical", product="OLE5")
    assert q.is_new
    options.describe(q.id, "Bugs and outages in Ole5.", who="Admin@T2.sa")
    assert not options.find("queue", "Ole5 Technical", product="OLE5").is_new
    options.set_hidden(q.id, True, who="admin@t2.sa")
    assert options.find("queue", "Ole5 Technical", product="OLE5") is None
    options.mark_seen(options.find("queue", "Ole5 Support", product="OLE5").id)
    options.set_guidance("queue", "Prefer the support queue when unsure.", who="admin@t2.sa")
    rows = db.query("SELECT who, action, what, name, old_value, new_value FROM audit_log "
                    "ORDER BY id")
    assert [(r["what"], r["name"], r["old_value"], r["new_value"]) for r in rows] == [
        ("queue description", "OLE5 / Ole5 Technical", None, "Bugs and outages in Ole5."),
        ("queue", "OLE5 / Ole5 Technical", "offered", "hidden"),
        ("guidance", "queue", None, "Prefer the support queue when unsure."),
    ]
    assert {r["who"] for r in rows} == {"admin@t2.sa"} and {r["action"] for r in rows} == {
        "change"}
    with pytest.raises(options.OptionError):
        options.describe(q.id, "x" * 501, who="admin@t2.sa")


def test_ticket_system_down_keeps_the_values(db, t2):
    options.sync(t2=t2)
    before = _count(db, "removed_at IS NULL")
    t2._auth = {"UserLogin": "x", "Password": "y"}
    r = options.sync(t2=t2, force=True)
    assert r.skipped and "AccessDenied" in r.skipped
    assert _count(db, "removed_at IS NULL") == before
