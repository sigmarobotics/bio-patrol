"""kachaka-sdk-toolkit v0.6.x compatibility — fire-and-accept + callback slot.

v0.6.0 made KachakaCommands movement/shelf wrappers return on ACCEPT (not
completion) and made start_monitoring() update the single callback slot in
place instead of being a no-op while running. These tests pin the FleetAPI
behaviours that the upgrade would otherwise silently break.
"""
import asyncio
from unittest.mock import MagicMock

import pytest

from services.fleet_api import FleetAPI


def _wire_mocks(monkeypatch):
    fake_conn = MagicMock()
    fake_conn.ping.return_value = {"ok": True, "serial": "BKP"}
    fake_conn.serial = "BKP"
    fake_kc = MagicMock(get=MagicMock(return_value=fake_conn), remove=MagicMock())
    monkeypatch.setattr("services.fleet_api.KachakaConnection", fake_kc)
    fake_ctrl = MagicMock()
    monkeypatch.setattr(
        "services.fleet_api.RobotController", MagicMock(return_value=fake_ctrl)
    )
    fake_cmds = MagicMock()
    monkeypatch.setattr(
        "services.fleet_api.KachakaCommands", MagicMock(return_value=fake_cmds)
    )
    monkeypatch.setattr("services.fleet_api.KachakaQueries", MagicMock())
    return fake_conn, fake_ctrl, fake_cmds, fake_kc


@pytest.mark.asyncio
async def test_fleet_callback_registered_after_ctrl_start(monkeypatch):
    """ctrl.start() must run BEFORE the fleet registers its callback.

    Since toolkit v0.6.0, start_monitoring() replaces the single callback
    slot in place. ctrl.start() internally re-registers the controller's
    own callback, so if it runs last it overwrites the fleet's — killing
    slot.status / debouncer updates. The fleet callback must win the slot
    (it already delegates to the controller).
    """
    fake_conn, fake_ctrl, _, _ = _wire_mocks(monkeypatch)
    order = []
    fake_ctrl.start.side_effect = lambda: order.append("ctrl.start")
    fake_conn.start_monitoring.side_effect = (
        lambda *a, **kw: order.append(
            "fleet_callback" if "on_state_change" in kw else "monitor"
        )
    )

    fleet = FleetAPI()
    await fleet.register_robot("kachaka", "1.2.3.4")

    assert "ctrl.start" in order and "fleet_callback" in order
    assert order.index("ctrl.start") < order.index("fleet_callback"), (
        f"fleet callback must be registered after ctrl.start, got {order}"
    )


@pytest.mark.asyncio
async def test_move_to_pose_uses_controller(monkeypatch):
    """move_to_pose must go through RobotController (blocking with timeout),
    not KachakaCommands (fire-and-accept since v0.6.0) — otherwise the
    TaskEngine advances to the next step before the robot arrives."""
    _, fake_ctrl, fake_cmds, _ = _wire_mocks(monkeypatch)
    fake_ctrl.move_to_pose.return_value = {"ok": True}

    fleet = FleetAPI()
    await fleet.register_robot("kachaka", "1.2.3.4")
    result = await fleet.move_to_pose("kachaka", 1.0, 2.0, 0.5)

    fake_ctrl.move_to_pose.assert_called_once()
    assert fake_ctrl.move_to_pose.call_args.args[:3] == (1.0, 2.0, 0.5)
    fake_cmds.move_to_pose.assert_not_called()
    assert result == {"ok": True}


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["dock_shelf", "undock_shelf"])
async def test_dock_undock_polls_until_complete(monkeypatch, method):
    """dock/undock results must reflect COMPLETION, not acceptance."""
    _, _, fake_cmds, _ = _wire_mocks(monkeypatch)
    getattr(fake_cmds, method).return_value = {"ok": True}  # accepted
    fake_cmds.poll_until_complete.return_value = {"ok": True, "error_code": 0}

    fleet = FleetAPI()
    await fleet.register_robot("kachaka", "1.2.3.4")
    result = await getattr(fleet, method)("kachaka")

    fake_cmds.poll_until_complete.assert_called_once()
    assert result == {"ok": True, "error_code": 0}


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["dock_shelf", "undock_shelf"])
async def test_dock_undock_skips_poll_on_rejected_accept(monkeypatch, method):
    """A rejected command must surface the accept error without polling."""
    _, _, fake_cmds, _ = _wire_mocks(monkeypatch)
    rejected = {"ok": False, "error_code": 10250}
    getattr(fake_cmds, method).return_value = rejected

    fleet = FleetAPI()
    await fleet.register_robot("kachaka", "1.2.3.4")
    result = await getattr(fleet, method)("kachaka")

    fake_cmds.poll_until_complete.assert_not_called()
    assert result == rejected


@pytest.mark.asyncio
async def test_register_failure_removes_connection(monkeypatch):
    """Failed ping must tear down the pooled connection so the auto-started
    monitor thread (v0.6.0) doesn't keep pinging a bad IP forever.
    (KachakaConnection.remove stops monitoring since toolkit v0.6.1.)"""
    fake_conn, _, _, fake_kc = _wire_mocks(monkeypatch)
    fake_conn.ping.return_value = {"ok": False, "error": "DEADLINE_EXCEEDED"}

    fleet = FleetAPI()
    result = await fleet.register_robot("kachaka", "10.9.9.9")

    assert result["ok"] is False
    fake_kc.remove.assert_called_once_with("10.9.9.9")
