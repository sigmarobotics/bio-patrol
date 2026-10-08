"""/demo view edits the demo route, never the real patrol route.

Requests carrying ``X-Bio-Data: demo`` read and write the demo preset
(settings ``demo_preset``) through /api/patrol and the preset save/load
endpoints; everything else keeps using patrol.json.
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

import routers.patrol as patrol

DEMO = {"X-Bio-Data": "demo"}
REAL_ROUTE = {"beds_order": [{"bed_key": "301-6", "enabled": True}]}
DEMO_ROUTE = {"beds_order": [{"bed_key": "301-1", "enabled": True},
                             {"bed_key": "301-2", "enabled": True}]}


@pytest.fixture
def env(tmp_path, monkeypatch):
    patrol_file = tmp_path / "patrol.json"
    presets = tmp_path / "patrol_presets"
    presets.mkdir()
    patrol_file.write_text(json.dumps(REAL_ROUTE))
    (presets / "xinmin-demo3.json").write_text(json.dumps(DEMO_ROUTE))
    settings = {"demo_preset": "xinmin-demo3"}
    monkeypatch.setattr(patrol, "PATROL_FILE", str(patrol_file))
    monkeypatch.setattr(patrol, "PATROL_PRESETS_DIR", str(presets))
    monkeypatch.setattr(patrol, "get_runtime_settings", lambda: dict(settings))
    monkeypatch.setattr(patrol, "update_settings", lambda **kw: settings.update(kw))
    return patrol_file, presets, settings


@pytest.fixture
def client():
    from main import app
    return TestClient(app)


def _read(path):
    return json.loads(path.read_text())


def test_get_patrol_with_header_returns_demo_route(env, client):
    assert client.get("/api/patrol", headers=DEMO).json() == DEMO_ROUTE


def test_get_patrol_without_header_returns_real_route(env, client):
    assert client.get("/api/patrol").json() == REAL_ROUTE


def test_post_patrol_with_header_writes_demo_preset_only(env, client):
    patrol_file, presets, _ = env
    new = {"beds_order": [{"bed_key": "301-3", "enabled": True}]}
    assert client.post("/api/patrol", json=new, headers=DEMO).status_code == 200
    assert _read(presets / "xinmin-demo3.json") == new
    assert _read(patrol_file) == REAL_ROUTE


def test_post_patrol_without_header_writes_real_route(env, client):
    patrol_file, presets, _ = env
    new = {"beds_order": [{"bed_key": "301-9", "enabled": True}]}
    client.post("/api/patrol", json=new)
    assert _read(patrol_file) == new
    assert _read(presets / "xinmin-demo3.json") == DEMO_ROUTE


def test_no_demo_preset_reads_real_route_and_first_write_creates_one(env, client):
    patrol_file, presets, settings = env
    settings["demo_preset"] = ""
    assert client.get("/api/patrol", headers=DEMO).json() == REAL_ROUTE
    new = {"beds_order": [{"bed_key": "301-4", "enabled": True}]}
    client.post("/api/patrol", json=new, headers=DEMO)
    assert settings["demo_preset"] == "demo"
    assert _read(presets / "demo.json") == new
    assert _read(patrol_file) == REAL_ROUTE


def test_save_as_with_header_snapshots_demo_route(env, client):
    _, presets, _ = env
    client.post("/api/patrol/presets/snap", headers=DEMO)
    assert _read(presets / "snap.json") == DEMO_ROUTE


def test_load_with_header_loads_into_demo_route(env, client):
    patrol_file, presets, _ = env
    other = {"beds_order": [{"bed_key": "301-7", "enabled": True}]}
    (presets / "other.json").write_text(json.dumps(other))
    client.post("/api/patrol/presets/other/load", headers=DEMO)
    assert _read(presets / "xinmin-demo3.json") == other
    assert _read(patrol_file) == REAL_ROUTE
