"""Unit tests for button_db — uses temp sqlite files, no shared state."""
from __future__ import annotations

import os
import tempfile

import pytest

from services import button_db


@pytest.fixture
def tmp_db():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "buttons.db")
        button_db.init_schema(path)
        button_db.seed_actions(["demo_run", "shelf_resume", "speak"], path)
        yield path


def test_seed_creates_rows_for_each_action(tmp_db):
    rows = button_db.list_bindings(tmp_db)
    assert {r["action_key"] for r in rows} == {"demo_run", "shelf_resume", "speak"}
    assert all(r["ieee_addr"] is None for r in rows)


def test_seed_is_idempotent(tmp_db):
    button_db.seed_actions(["demo_run"], tmp_db)
    rows = button_db.list_bindings(tmp_db)
    assert sum(1 for r in rows if r["action_key"] == "demo_run") == 1


def test_bind_action_sets_ieee(tmp_db):
    button_db.bind_action("demo_run", "0xAA", "demo_btn", tmp_db)
    row = button_db.get_binding_by_ieee("0xAA", tmp_db)
    assert row is not None
    assert row["action_key"] == "demo_run"
    assert row["friendly_name"] == "demo_btn"
    assert row["paired_at"]


def test_bind_clears_other_action_holding_same_ieee(tmp_db):
    button_db.bind_action("demo_run", "0xAA", None, tmp_db)
    button_db.bind_action("speak", "0xAA", None, tmp_db)
    rows = {r["action_key"]: r for r in button_db.list_bindings(tmp_db)}
    assert rows["demo_run"]["ieee_addr"] is None
    assert rows["speak"]["ieee_addr"] == "0xAA"


def test_bind_to_unseeded_action_creates_row(tmp_db):
    button_db.bind_action("future_action", "0xBB", None, tmp_db)
    rows = {r["action_key"]: r for r in button_db.list_bindings(tmp_db)}
    assert rows["future_action"]["ieee_addr"] == "0xBB"


def test_unbind_returns_previous_ieee_and_clears(tmp_db):
    button_db.bind_action("demo_run", "0xAA", "demo_btn", tmp_db)
    prev = button_db.unbind_action("demo_run", tmp_db)
    assert prev == "0xAA"
    rows = {r["action_key"]: r for r in button_db.list_bindings(tmp_db)}
    assert rows["demo_run"]["ieee_addr"] is None
    assert rows["demo_run"]["paired_at"] is None


def test_unbind_when_not_bound_returns_none(tmp_db):
    assert button_db.unbind_action("demo_run", tmp_db) is None


def test_update_status_sets_battery_and_last_seen(tmp_db):
    button_db.bind_action("demo_run", "0xAA", None, tmp_db)
    button_db.update_status("0xAA", 75, "2026-04-30T10:00:00", tmp_db)
    row = button_db.get_binding_by_ieee("0xAA", tmp_db)
    assert row["battery"] == 75
    assert row["last_seen"] == "2026-04-30T10:00:00"


def test_update_status_preserves_battery_when_none(tmp_db):
    button_db.bind_action("demo_run", "0xAA", None, tmp_db)
    button_db.update_status("0xAA", 60, "t1", tmp_db)
    button_db.update_status("0xAA", None, "t2", tmp_db)
    row = button_db.get_binding_by_ieee("0xAA", tmp_db)
    assert row["battery"] == 60
    assert row["last_seen"] == "t2"


def test_record_fire_increments_counter(tmp_db):
    button_db.bind_action("demo_run", "0xAA", None, tmp_db)
    button_db.record_fire("demo_run", tmp_db)
    button_db.record_fire("demo_run", tmp_db)
    rows = {r["action_key"]: r for r in button_db.list_bindings(tmp_db)}
    assert rows["demo_run"]["fire_count"] == 2
    assert rows["demo_run"]["last_fired_at"]


def test_unique_ieee_index_blocks_two_actions_holding_same_ieee(tmp_db):
    """The unique partial index is enforced via the bind_action helper which
    nulls the other row first; manual concurrent inserts would still violate
    the constraint, but we don't exercise that path."""
    button_db.bind_action("demo_run", "0xAA", None, tmp_db)
    button_db.bind_action("speak", "0xAA", None, tmp_db)
    rows = button_db.list_bindings(tmp_db)
    bound_count = sum(1 for r in rows if r["ieee_addr"] == "0xAA")
    assert bound_count == 1
