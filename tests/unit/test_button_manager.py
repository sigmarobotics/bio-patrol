"""Unit tests for ButtonManager — fakes the Zigbee MQTT client + DB path."""
from __future__ import annotations

import asyncio
import os
import tempfile
import time

import pytest

from services import action_registry, button_db, button_manager
from services.button_manager import ButtonManager


class FakeZigbee:
    def __init__(self):
        self.connected = True
        self.permit_calls: list[tuple[bool, int]] = []
        self.removed: list[str] = []

    async def permit_join(self, allow: bool, time_s: int = 120) -> bool:
        self.permit_calls.append((allow, time_s))
        return True

    async def remove_device(self, ieee: str) -> bool:
        self.removed.append(ieee)
        return True


@pytest.fixture
def db_path():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "buttons.db")
        button_db.init_schema(path)
        action_registry.reset()
        yield path
        action_registry.reset()


@pytest.fixture
def fired():
    return []


@pytest.fixture
def mgr(db_path, fired):
    async def _h(params):
        fired.append(params)
        return {"ok": True}

    action_registry.register("demo_run", "Demo", _h)
    button_db.seed_actions(["demo_run"], db_path)

    fake = FakeZigbee()
    return ButtonManager(fake, db_path=db_path)


def test_arm_pair_sets_target(mgr):
    result = asyncio.run(mgr.arm_pair("demo_run", time_s=60))
    assert result == {"ok": True, "action_key": "demo_run", "timeout": 60}
    assert mgr.pairing_target == "demo_run"
    assert mgr.zigbee.permit_calls == [(True, 60)]


def test_arm_pair_unknown_action_rejected(mgr):
    result = asyncio.run(mgr.arm_pair("not_an_action"))
    assert result["ok"] is False
    assert "unknown action" in result["error"]
    assert mgr.zigbee.permit_calls == []


def test_cancel_pair_clears_target(mgr):
    asyncio.run(mgr.arm_pair("demo_run"))
    asyncio.run(mgr.cancel_pair())
    assert mgr.pairing_target is None
    # last permit call should disable join
    assert mgr.zigbee.permit_calls[-1] == (False, 0)


def test_device_joined_during_pair_window_binds(mgr, db_path):
    asyncio.run(mgr.arm_pair("demo_run"))
    event = {"type": "device_joined", "ieee_addr": "0xAA", "friendly_name": "demo_btn"}
    asyncio.run(mgr.handle_event(event))
    row = button_db.get_binding_by_ieee("0xAA", db_path)
    assert row["action_key"] == "demo_run"
    assert mgr.pairing_target is None  # consumed


def test_device_joined_without_arming_is_ignored(mgr, db_path):
    event = {"type": "device_joined", "ieee_addr": "0xAA", "friendly_name": "stray"}
    asyncio.run(mgr.handle_event(event))
    assert button_db.get_binding_by_ieee("0xAA", db_path) is None


def test_pair_target_expires_silently(mgr, monkeypatch):
    asyncio.run(mgr.arm_pair("demo_run", time_s=1))
    # advance the monotonic clock past expiry
    real_monotonic = time.monotonic()
    monkeypatch.setattr(time, "monotonic", lambda: real_monotonic + 2.0)
    assert mgr.pairing_target is None


def test_already_bound_device_rejoin_silent_readmit(mgr, db_path):
    # Device was paired before; now it announces a join (e.g. after a sleep
    # cycle). No pair_target armed — should silently re-admit and update status.
    button_db.bind_action("demo_run", "0xAA", "demo_btn", db_path)
    event = {"type": "device_joined", "ieee_addr": "0xAA",
             "friendly_name": "demo_btn"}
    asyncio.run(mgr.handle_event(event))
    row = button_db.get_binding_by_ieee("0xAA", db_path)
    assert row["action_key"] == "demo_run"
    # rejoin must NOT trigger another permit_join publish
    permit_calls_for_pair = [
        c for c in mgr.zigbee.permit_calls if c == (False, 0)
    ]
    assert permit_calls_for_pair == []


def test_button_press_fires_bound_action(mgr, db_path, fired):
    button_db.bind_action("demo_run", "0xAA", "demo_btn", db_path)
    event = {"type": "button_action", "ieee_addr": "0xAA", "action": "single",
             "battery": 90, "linkquality": 200}
    asyncio.run(mgr.handle_event(event))
    assert fired == [{}]
    row = button_db.get_binding_by_ieee("0xAA", db_path)
    assert row["fire_count"] == 1
    assert row["battery"] == 90


def test_double_press_is_ignored(mgr, db_path, fired):
    button_db.bind_action("demo_run", "0xAA", None, db_path)
    event = {"type": "button_action", "ieee_addr": "0xAA", "action": "double"}
    asyncio.run(mgr.handle_event(event))
    assert fired == []


def test_unbound_button_press_does_not_fire(mgr, fired):
    event = {"type": "button_action", "ieee_addr": "0xUNK", "action": "single"}
    asyncio.run(mgr.handle_event(event))
    assert fired == []


def test_repeat_press_within_debounce_window_dropped(mgr, db_path, fired):
    button_db.bind_action("demo_run", "0xAA", None, db_path)
    event = {"type": "button_action", "ieee_addr": "0xAA", "action": "single"}
    asyncio.run(mgr.handle_event(event))
    asyncio.run(mgr.handle_event(event))
    assert len(fired) == 1


def test_repeat_press_after_debounce_fires(mgr, db_path, fired, monkeypatch):
    button_db.bind_action("demo_run", "0xAA", None, db_path)
    event = {"type": "button_action", "ieee_addr": "0xAA", "action": "single"}
    base = time.monotonic()
    monkeypatch.setattr(time, "monotonic", lambda: base)
    asyncio.run(mgr.handle_event(event))
    monkeypatch.setattr(time, "monotonic", lambda: base + button_manager.DEBOUNCE_SECONDS + 0.5)
    asyncio.run(mgr.handle_event(event))
    assert len(fired) == 2
