"""IT-21b: /demo is the standard SPA; requests carrying ``X-Bio-Data: demo``
read demo_data.db, everything else reads sensor_data.db and never sees a demo
row.

The demo DB is a temp file (tests/unit/conftest.py ``_isolated_demo_db``).
"""
from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient

from services import demo_data
from settings.defaults import DEFAULT_SETTINGS

DEMO = {"X-Bio-Data": "demo"}
CFG = dict(DEFAULT_SETTINGS)


def _init_real(db_path):
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE sensor_scan_data (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT, location_id TEXT, bed_name TEXT, timestamp TEXT,
            retry_count INTEGER, status INTEGER, bpm INTEGER, rpm INTEGER,
            is_valid INTEGER, data_json TEXT, details TEXT
        )
        """
    )
    conn.execute(
        """
        INSERT INTO sensor_scan_data
        (task_id, location_id, bed_name, timestamp, retry_count, status, bpm, rpm,
         is_valid, data_json, details)
        VALUES ('20261001100000-aaaaaa', 'loc_101_1', '101-1', '2026-10-01T10:00:00',
                0, 4, 61, 11, 1, '{}', '量測正常')
        """
    )
    conn.commit()
    conn.close()


@pytest.fixture
def real_db(tmp_path, monkeypatch):
    path = str(tmp_path / "sensor_data.db")
    _init_real(path)

    class _Stub:
        db_path = path
        latest_data = None

    monkeypatch.setattr("routers.bio_sensor.get_bio_sensor_client", lambda: _Stub())
    return path


@pytest.fixture
def client():
    from main import app
    return TestClient(app)


def _seed_demo():
    """One demo run on 101-1 (abnormal) and 101-2 (absent)."""
    tid = "20261008100000-d3m0aa"
    demo_data.save_scan(tid, "101-1", "loc_101_1", "abnormal",
                        {"status": 4, "bpm": 133, "rpm": 34}, CFG)
    demo_data.save_scan(tid, "101-2", "loc_101_2", "absent",
                        {"status": 0, "bpm": 0, "rpm": 0}, CFG)
    return tid


# ── latest-by-bed ───────────────────────────────────────────────────────────

def test_latest_by_bed_with_header_reads_demo_rows(real_db, client):
    tid = _seed_demo()
    body = client.get("/api/bio-sensor/latest-by-bed", headers=DEMO).json()
    assert body["status"] == "success"
    by_bed = {r["bed_name"]: r for r in body["data"]}
    assert set(by_bed) == {"101-1", "101-2"}
    assert by_bed["101-1"]["bpm"] == 133 and by_bed["101-1"]["task_id"] == tid
    assert by_bed["101-2"]["is_valid"] is False


def test_latest_by_bed_without_header_never_returns_demo_rows(real_db, client):
    _seed_demo()
    body = client.get("/api/bio-sensor/latest-by-bed").json()
    assert body["status"] == "success"
    assert [(r["bed_name"], r["bpm"]) for r in body["data"]] == [("101-1", 61)]


# ── scan-history ────────────────────────────────────────────────────────────

def test_scan_history_with_header_reads_demo_rows(real_db, client):
    tid = _seed_demo()
    body = client.get("/api/bio-sensor/scan-history", headers=DEMO).json()
    assert body["status"] == "success"
    assert {r["task_id"] for r in body["data"]} == {tid}
    assert body["count"] == 2
    # Same task_id prefix filter the history tab uses.
    body = client.get(f"/api/bio-sensor/scan-history?task_id={tid[:14]}",
                      headers=DEMO).json()
    assert body["count"] == 2
    body = client.get("/api/bio-sensor/scan-history?location_id=loc_101_2",
                      headers=DEMO).json()
    assert [r["bed_name"] for r in body["data"]] == ["101-2"]


def test_scan_history_without_header_never_returns_demo_rows(real_db, client):
    _seed_demo()
    body = client.get("/api/bio-sensor/scan-history").json()
    assert [r["task_id"] for r in body["data"]] == ["20261001100000-aaaaaa"]


# ── bed-stats ───────────────────────────────────────────────────────────────

def test_bed_stats_with_header_reads_demo_rows(real_db, client):
    demo_data.ensure_history([{"bed_key": "101-1", "location_id": "loc_101_1"}], CFG)
    _seed_demo()
    body = client.get("/api/bio-sensor/bed-stats?location_id=loc_101_1&bed_name=101-1&window=200",
                      headers=DEMO).json()
    assert body["status"] == "success"
    assert body["stats"]["valid_count"] == 31     # 30 history days + today's run
    assert body["trend"][-1]["bpm"] == 133


def test_bed_stats_without_header_never_returns_demo_rows(real_db, client):
    demo_data.ensure_history([{"bed_key": "101-1", "location_id": "loc_101_1"}], CFG)
    _seed_demo()
    body = client.get("/api/bio-sensor/bed-stats?location_id=loc_101_1&bed_name=101-1").json()
    assert body["stats"]["valid_count"] == 1
    assert body["trend"] == [{"timestamp": "2026-10-01T10:00:00", "bpm": 61, "rpm": 11}]


# ── empty demo DB / no bio-sensor client ────────────────────────────────────

def test_empty_demo_db_returns_empty_results(real_db, client):
    latest = client.get("/api/bio-sensor/latest-by-bed", headers=DEMO).json()
    assert latest == {"status": "success", "data": [], "count": 0}
    hist = client.get("/api/bio-sensor/scan-history", headers=DEMO).json()
    assert hist == {"status": "success", "data": [], "count": 0}
    stats = client.get("/api/bio-sensor/bed-stats?location_id=loc_101_1", headers=DEMO).json()
    assert stats["status"] == "success"
    assert stats["stats"]["valid_count"] == 0 and stats["trend"] == []


def test_demo_reads_work_without_a_bio_sensor_client(monkeypatch, client):
    monkeypatch.setattr("routers.bio_sensor.get_bio_sensor_client", lambda: None)
    _seed_demo()
    assert client.get("/api/bio-sensor/latest-by-bed", headers=DEMO).json()["count"] == 2
    assert client.get("/api/bio-sensor/scan-history", headers=DEMO).json()["count"] == 2
    stats = client.get("/api/bio-sensor/bed-stats?location_id=loc_101_1", headers=DEMO).json()
    assert stats["status"] == "success"
    # Without the header the MQTT-disabled behaviour is unchanged.
    assert client.get("/api/bio-sensor/latest-by-bed").json()["status"] == "disabled"


# ── /demo serves the standard SPA ───────────────────────────────────────────

@pytest.mark.parametrize("path", ["/demo", "/demo/"])
def test_demo_path_serves_the_standard_index_with_a_root_base(client, path):
    res = client.get(path, follow_redirects=False)
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("text/html")
    assert res.headers["cache-control"] == "no-cache, must-revalidate"
    html = res.text
    assert '<head>\n<base href="/">' in html or '<head><base href="/">' in html
    # It IS the standard SPA — same scripts, no demo-only assets.
    assert 'src="js/script.js' in html and 'src="js/dataService.js' in html
    assert "demo.js" not in html and "demo.css" not in html


def test_custom_demo_api_is_gone(client):
    assert client.get("/api/demo/live").status_code == 404


# ── out-of-band flag on latest-by-bed (bed card 心跳呼吸異常) ──────────────

def test_latest_by_bed_demo_flags_out_of_band_regardless_of_switch(real_db, client, monkeypatch):
    monkeypatch.setattr("routers.bio_sensor.get_runtime_settings",
                        lambda: {**CFG, "vitals_alert_enabled": False})
    _seed_demo()
    by_bed = {r["bed_name"]: r for r in
              client.get("/api/bio-sensor/latest-by-bed", headers=DEMO).json()["data"]}
    assert by_bed["101-1"]["out_of_band"] is True     # 133/34, valid
    assert by_bed["101-2"]["out_of_band"] is False    # absent, not a valid reading


@pytest.mark.parametrize("enabled", [False, True])
def test_latest_by_bed_real_flags_out_of_band_only_when_enabled(real_db, client, monkeypatch, enabled):
    conn = sqlite3.connect(real_db)
    conn.execute("UPDATE sensor_scan_data SET bpm = 140")
    conn.commit()
    conn.close()
    monkeypatch.setattr("routers.bio_sensor.get_runtime_settings",
                        lambda: {**CFG, "vitals_alert_enabled": enabled})
    row = client.get("/api/bio-sensor/latest-by-bed").json()["data"][0]
    assert row["out_of_band"] is enabled
