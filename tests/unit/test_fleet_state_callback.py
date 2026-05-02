import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.fleet_api import FleetAPI


@pytest.mark.asyncio
async def test_register_starts_monitoring_with_fleet_callback(monkeypatch):
    fake_conn = MagicMock()
    fake_conn.ping.return_value = {"ok": True, "serial": "BKP"}
    fake_conn.serial = "BKP"

    monkeypatch.setattr(
        "services.fleet_api.KachakaConnection",
        MagicMock(get=MagicMock(return_value=fake_conn)),
    )
    fake_ctrl = MagicMock()
    monkeypatch.setattr(
        "services.fleet_api.RobotController", MagicMock(return_value=fake_ctrl)
    )
    monkeypatch.setattr("services.fleet_api.KachakaCommands", MagicMock())
    monkeypatch.setattr("services.fleet_api.KachakaQueries", MagicMock())

    fleet = FleetAPI()
    await fleet.register_robot("kachaka", "1.2.3.4")

    fake_conn.start_monitoring.assert_called_once()
    kwargs = fake_conn.start_monitoring.call_args.kwargs
    assert "on_state_change" in kwargs
    assert callable(kwargs["on_state_change"])
    fake_ctrl.start.assert_called_once()
    from services.fleet_api import RobotController as RC
    rc_kwargs = RC.call_args.kwargs
    assert callable(rc_kwargs["on_shelf_dropped"])


@pytest.mark.asyncio
async def test_state_callback_mirrors_to_slot_and_delegates_to_controller(monkeypatch):
    fake_conn = MagicMock()
    fake_conn.ping.return_value = {"ok": True, "serial": "BKP"}
    fake_conn.serial = "BKP"
    monkeypatch.setattr(
        "services.fleet_api.KachakaConnection",
        MagicMock(get=MagicMock(return_value=fake_conn)),
    )
    fake_ctrl = MagicMock()
    monkeypatch.setattr(
        "services.fleet_api.RobotController", MagicMock(return_value=fake_ctrl)
    )
    monkeypatch.setattr("services.fleet_api.KachakaCommands", MagicMock())
    monkeypatch.setattr("services.fleet_api.KachakaQueries", MagicMock())

    from kachaka_core.connection import ConnectionState

    fleet = FleetAPI()
    await fleet.register_robot("kachaka", "1.2.3.4")
    cb = fake_conn.start_monitoring.call_args.kwargs["on_state_change"]

    cb(ConnectionState.DISCONNECTED)
    slot = fleet._robots["kachaka"]
    assert slot.status == "offline"
    fake_ctrl._on_conn_state_change.assert_called_with(ConnectionState.DISCONNECTED)

    cb(ConnectionState.CONNECTED)
    assert slot.status == "online"


@pytest.mark.asyncio
async def test_unregister_shuts_down_debouncer(monkeypatch):
    fake_conn = MagicMock()
    fake_conn.ping.return_value = {"ok": True, "serial": "BKP"}
    fake_conn.serial = "BKP"
    monkeypatch.setattr(
        "services.fleet_api.KachakaConnection",
        MagicMock(get=MagicMock(return_value=fake_conn), remove=MagicMock()),
    )
    monkeypatch.setattr("services.fleet_api.RobotController", MagicMock())
    monkeypatch.setattr("services.fleet_api.KachakaCommands", MagicMock())
    monkeypatch.setattr("services.fleet_api.KachakaQueries", MagicMock())

    fleet = FleetAPI()
    await fleet.register_robot("kachaka", "1.2.3.4")
    slot = fleet._robots["kachaka"]
    slot.debouncer = MagicMock()
    slot.debouncer.shutdown = AsyncMock()

    await fleet.unregister_robot("kachaka")

    slot.debouncer.shutdown.assert_awaited()


