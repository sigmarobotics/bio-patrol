"""A failed return_shelf whose shelf/location list read fails is "unknown".

kachaka_core's @with_retry does not raise once retries run out — it returns
{"ok": False, "error": "UNAVAILABLE: ..."}. _shelf_dropped_en_route used to
see no "shelves" key, find no shelf and report a drop without flagging the
disconnect, so a link that recovered before the classify reads paged the
ward with a CRITICAL 貨架掉落 (audit 2026-10-27). The rule (PR #34,
2026-08-10): a failed read is never a drop.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

from services import task_runtime

UNREACHABLE_READ = {
    "ok": False,
    "error": "UNAVAILABLE: failed to connect to all addresses; No route to host",
    "retryable": True,
    "attempts": 3,
}
HEALTHY_BATTERY = {"ok": True, "percentage": 74.0, "power_status": "2"}
IDLE_CMD = {"ok": True, "state": "1", "command": None, "is_running": False}
SHELF = {"id": "S_04", "name": "S_04", "home_location_id": "L01"}


def _engine(shelves, locations=None):
    fleet = MagicMock()
    slot = MagicMock()
    slot.conn.client.get_moving_shelf_id = MagicMock(return_value="")
    fleet.get_slot_or_none = MagicMock(return_value=slot)
    fleet.get_shelves = AsyncMock(return_value=shelves)
    fleet.get_locations = AsyncMock(return_value=locations or {"ok": True, "locations": []})
    # The link is back by the time the classify reads run.
    fleet.get_battery_info = AsyncMock(return_value=HEALTHY_BATTERY)
    fleet.get_command_state = AsyncMock(return_value=IDLE_CMD)
    return task_runtime.TaskEngine(fleet, "kachaka")


def _check_then_classify(eng):
    async def go():
        dropped = await eng._shelf_dropped_en_route("S_04")
        kind, _ = await eng._classify_release(None)
        return dropped, kind
    return asyncio.run(go())


def test_unreachable_shelf_list_is_disconnect_not_drop():
    eng = _engine(UNREACHABLE_READ)

    dropped, kind = _check_then_classify(eng)

    assert dropped is True              # the run still stops
    assert eng._disconnect_suspected is True
    assert kind == "disconnected"       # never a CRITICAL drop


def test_unreachable_location_list_is_disconnect_not_drop():
    eng = _engine({"ok": True, "shelves": [SHELF]}, UNREACHABLE_READ)

    dropped, kind = _check_then_classify(eng)

    assert dropped is True
    assert eng._disconnect_suspected is True
    assert kind == "disconnected"


def test_repeated_unreachable_reads_never_become_a_drop():
    eng = _engine(UNREACHABLE_READ)

    for _ in range(3):
        dropped, kind = _check_then_classify(eng)
        assert dropped is True
        assert eng._disconnect_suspected is True
        assert kind == "disconnected"
    assert eng.fleet.get_shelves.await_count == 3


def test_non_connection_read_error_stays_a_drop():
    eng = _engine({"ok": False, "error": "error_code=11005: shelf pose mismatch"})

    dropped, kind = _check_then_classify(eng)

    assert dropped is True
    assert eng._disconnect_suspected is False
    assert kind == "drop"


def test_successful_but_empty_shelf_list_is_still_a_drop():
    """Regression guard: the robot answered and the shelf is not there."""
    eng = _engine({"ok": True, "shelves": []})

    dropped, kind = _check_then_classify(eng)

    assert dropped is True
    assert eng._disconnect_suspected is False
    assert kind == "drop"
