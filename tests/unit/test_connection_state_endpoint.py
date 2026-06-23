from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from main import app
from dependencies import get_fleet

_BASE = {
    "robot_id": "kachaka",
    "ip": "192.168.50.133:26400",
    "serial": "BKP",
    "last_seen": 1714638200.0,
    "disconnected_at": None,
    "last_reconnect_at": None,
    "in_patrol": False,
    "debounce_seconds": 300,
    "offline_pending": False,
}


@pytest.fixture
def client(monkeypatch):
    fake_fleet = MagicMock()
    app.dependency_overrides[get_fleet] = lambda: fake_fleet
    yield TestClient(app), fake_fleet
    app.dependency_overrides.clear()


def test_connection_state_unregistered(client):
    c, fleet = client
    fleet.get_connection_state_dict.return_value = {
        **_BASE, "state": "unregistered", "ip": "", "serial": "",
        "last_seen": None, "in_patrol": False,
    }
    r = c.get("/api/robot/connection-state")
    assert r.status_code == 200
    assert r.json()["state"] == "unregistered"


def test_connection_state_connected(client):
    c, fleet = client
    fleet.get_connection_state_dict.return_value = {
        **_BASE, "state": "connected", "in_patrol": True,
    }
    r = c.get("/api/robot/connection-state")
    assert r.status_code == 200
    body = r.json()
    assert body["state"] == "connected"
    assert body["in_patrol"] is True
    assert body["debounce_seconds"] >= 1


def test_connection_state_disconnected_with_pending(client):
    c, fleet = client
    fleet.get_connection_state_dict.return_value = {
        **_BASE, "state": "disconnected", "disconnected_at": 2.0, "offline_pending": True,
    }
    r = c.get("/api/robot/connection-state")
    assert r.status_code == 200
    body = r.json()
    assert body["state"] == "disconnected"
    assert body["offline_pending"] is True


def test_connection_state_no_debouncer(client):
    c, fleet = client
    fleet.get_connection_state_dict.return_value = {
        **_BASE, "state": "connected", "offline_pending": False,
    }
    r = c.get("/api/robot/connection-state")
    assert r.status_code == 200
    assert r.json()["offline_pending"] is False
