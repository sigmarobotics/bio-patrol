from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from main import app
from dependencies import get_fleet


@pytest.fixture
def client(monkeypatch):
    fake_fleet = MagicMock()
    monkeypatch.setattr("dependencies.get_fleet", lambda: fake_fleet)
    app.dependency_overrides[get_fleet] = lambda: fake_fleet
    yield TestClient(app), fake_fleet
    app.dependency_overrides.clear()


def test_connection_state_unregistered(client):
    c, fleet = client
    fleet.get_slot_or_none = MagicMock(return_value=None)
    r = c.get("/api/robot/connection-state")
    assert r.status_code == 200
    body = r.json()
    assert body["state"] == "unregistered"


def test_connection_state_connected(client):
    from kachaka_core.connection import ConnectionState

    c, fleet = client
    slot = MagicMock()
    slot.robot_id = "kachaka"
    slot.ip = "192.168.50.133:26400"
    slot.serial = "BKP"
    slot.last_seen = 1714638200.0
    slot.disconnected_at = None
    slot.last_reconnect_at = None
    slot.conn = MagicMock()
    slot.conn.state = ConnectionState.CONNECTED
    slot.debouncer = MagicMock()
    slot.debouncer.is_offline_pending = MagicMock(return_value=False)
    fleet.get_slot_or_none = MagicMock(return_value=slot)

    monkey_current = {"kachaka": "task-123"}
    import services.task_runtime as tr
    tr.current_tasks.clear()
    tr.current_tasks.update(monkey_current)

    r = c.get("/api/robot/connection-state")
    body = r.json()
    assert body["state"] == "connected"
    assert body["in_patrol"] is True
    assert body["debounce_seconds"] >= 1
    tr.current_tasks.clear()


def test_connection_state_disconnected_with_pending(client):
    from kachaka_core.connection import ConnectionState

    c, fleet = client
    slot = MagicMock()
    slot.robot_id = "kachaka"
    slot.ip = "ip"
    slot.serial = "BKP"
    slot.last_seen = 1.0
    slot.disconnected_at = 2.0
    slot.last_reconnect_at = None
    slot.conn = MagicMock()
    slot.conn.state = ConnectionState.DISCONNECTED

    slot.debouncer = MagicMock()
    slot.debouncer.is_offline_pending = MagicMock(return_value=True)
    fleet.get_slot_or_none = MagicMock(return_value=slot)

    r = c.get("/api/robot/connection-state")
    body = r.json()
    assert body["state"] == "disconnected"
    assert body["offline_pending"] is True


def test_connection_state_no_debouncer(client):
    from kachaka_core.connection import ConnectionState

    c, fleet = client
    slot = MagicMock()
    slot.robot_id = "kachaka"
    slot.ip = "ip"; slot.serial = ""; slot.last_seen = 0; slot.disconnected_at = None
    slot.last_reconnect_at = None
    slot.conn = MagicMock(); slot.conn.state = ConnectionState.CONNECTED
    slot.debouncer = None
    fleet.get_slot_or_none = MagicMock(return_value=slot)

    r = c.get("/api/robot/connection-state")
    body = r.json()
    assert body["offline_pending"] is False
