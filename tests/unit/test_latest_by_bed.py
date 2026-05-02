"""IT-9: GET /api/bio-sensor/latest-by-bed unit tests."""
import sqlite3
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


def _init_schema(db_path):
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute(
        """
        CREATE TABLE sensor_scan_data (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT, location_id TEXT, bed_name TEXT, timestamp TEXT,
            retry_count INTEGER, status INTEGER, bpm INTEGER, rpm INTEGER,
            is_valid INTEGER, data_json TEXT, details TEXT
        )
        """
    )
    conn.commit()
    conn.close()


@pytest.fixture
def client_with_db(monkeypatch):
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    db_path = tmp.name
    tmp.close()
    _init_schema(db_path)

    class _StubClient:
        def __init__(self, p):
            self.db_path = p
            # Per review feedback: latest_data must be an instance attribute,
            # not a class attribute, so each stub starts with a clean slate.
            self.latest_data = None

    stub = _StubClient(db_path)
    monkeypatch.setattr("routers.bio_sensor.get_bio_sensor_client", lambda: stub)

    from main import app

    yield TestClient(app), db_path
    Path(db_path).unlink(missing_ok=True)


def _insert(db_path, **kw):
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        INSERT INTO sensor_scan_data
        (task_id, location_id, bed_name, timestamp, retry_count, status, bpm, rpm, is_valid, data_json, details)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            kw["task_id"],
            kw["location_id"],
            kw["bed_name"],
            kw["timestamp"],
            kw.get("retry_count", 0),
            kw.get("status", 4),
            kw.get("bpm", 70),
            kw.get("rpm", 16),
            1 if kw.get("is_valid", True) else 0,
            kw.get("data_json", "{}"),
            kw.get("details", ""),
        ),
    )
    conn.commit()
    conn.close()


def test_latest_by_bed_empty_db(client_with_db):
    client, _ = client_with_db
    res = client.get("/api/bio-sensor/latest-by-bed")
    assert res.status_code == 200
    assert res.json() == {"status": "success", "data": [], "count": 0}


def test_latest_by_bed_single_bed_returns_newest(client_with_db):
    client, db = client_with_db
    _insert(db, task_id="T1", location_id="B_101-1", bed_name="101-1",
            timestamp="2026-05-01T10:00:00", bpm=70)
    _insert(db, task_id="T2", location_id="B_101-1", bed_name="101-1",
            timestamp="2026-05-01T11:00:00", bpm=72)
    res = client.get("/api/bio-sensor/latest-by-bed")
    assert res.json()["count"] == 1
    assert res.json()["data"][0]["bpm"] == 72  # newest


def test_latest_by_bed_multi_bed_each_newest(client_with_db):
    client, db = client_with_db
    _insert(db, task_id="T1", location_id="B_101-1", bed_name="101-1",
            timestamp="2026-05-01T10:00:00", bpm=70)
    _insert(db, task_id="T2", location_id="B_101-2", bed_name="101-2",
            timestamp="2026-05-01T10:30:00", bpm=80)
    _insert(db, task_id="T3", location_id="B_101-1", bed_name="101-1",
            timestamp="2026-05-01T11:00:00", bpm=72)
    res = client.get("/api/bio-sensor/latest-by-bed")
    by_bed = {r["bed_name"]: r for r in res.json()["data"]}
    assert by_bed["101-1"]["bpm"] == 72
    assert by_bed["101-2"]["bpm"] == 80


def test_latest_by_bed_filters_null_location(client_with_db):
    client, db = client_with_db
    _insert(db, task_id="T1", location_id="B_101-1", bed_name="101-1",
            timestamp="2026-05-01T10:00:00")
    _insert(db, task_id="T2", location_id=None, bed_name="orphan",
            timestamp="2026-05-01T11:00:00")
    res = client.get("/api/bio-sensor/latest-by-bed")
    assert res.json()["count"] == 1
    assert res.json()["data"][0]["location_id"] == "B_101-1"
