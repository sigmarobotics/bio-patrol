"""IT-9: GET /api/bio-sensor/scan-history?location_id=X filter tests."""
from tests.unit.test_latest_by_bed import client_with_db, _insert  # noqa: F401  (reuse fixtures)


def test_scan_history_filters_by_location_id(client_with_db):
    client, db = client_with_db
    _insert(db, task_id="T1", location_id="B_101-1", bed_name="101-1",
            timestamp="2026-05-01T10:00:00")
    _insert(db, task_id="T2", location_id="B_101-2", bed_name="101-2",
            timestamp="2026-05-01T10:30:00")
    res = client.get("/api/bio-sensor/scan-history?location_id=B_101-1")
    assert res.status_code == 200
    assert res.json()["count"] == 1
    assert res.json()["data"][0]["location_id"] == "B_101-1"


def test_scan_history_location_id_and_task_id_combined(client_with_db):
    client, db = client_with_db
    _insert(db, task_id="20260501100000", location_id="B_101-1", bed_name="101-1",
            timestamp="2026-05-01T10:00:00")
    _insert(db, task_id="20260501110000", location_id="B_101-1", bed_name="101-1",
            timestamp="2026-05-01T11:00:00")
    _insert(db, task_id="20260501100000", location_id="B_101-2", bed_name="101-2",
            timestamp="2026-05-01T10:00:00")
    res = client.get("/api/bio-sensor/scan-history?location_id=B_101-1&task_id=20260501100000")
    assert res.json()["count"] == 1
    assert res.json()["data"][0]["location_id"] == "B_101-1"
    assert res.json()["data"][0]["task_id"] == "20260501100000"


# ── IT-15: before_id cursor pagination ────────────────────────────────────────

def test_scan_history_before_id_returns_only_older_rows(client_with_db):
    client, db = client_with_db
    for i in range(3):
        _insert(db, task_id=f"T{i}", location_id="B_101-1", bed_name="101-1",
                timestamp=f"2026-05-01T1{i}:00:00")
    ids = [r["id"] for r in client.get("/api/bio-sensor/scan-history").json()["data"]]
    res = client.get(f"/api/bio-sensor/scan-history?before_id={ids[0]}")
    returned = [r["id"] for r in res.json()["data"]]
    assert returned == ids[1:]


def test_scan_history_before_id_combined_with_location_id(client_with_db):
    client, db = client_with_db
    _insert(db, task_id="T1", location_id="B_101-1", bed_name="101-1",
            timestamp="2026-05-01T10:00:00")
    _insert(db, task_id="T2", location_id="B_101-2", bed_name="101-2",
            timestamp="2026-05-01T11:00:00")
    _insert(db, task_id="T3", location_id="B_101-1", bed_name="101-1",
            timestamp="2026-05-01T12:00:00")
    newest = client.get("/api/bio-sensor/scan-history?location_id=B_101-1").json()["data"][0]
    res = client.get(f"/api/bio-sensor/scan-history?location_id=B_101-1&before_id={newest['id']}")
    assert res.json()["count"] == 1
    assert res.json()["data"][0]["task_id"] == "T1"


def test_scan_history_before_id_respects_limit(client_with_db):
    client, db = client_with_db
    for i in range(5):
        _insert(db, task_id=f"T{i}", location_id="B_101-1", bed_name="101-1",
                timestamp=f"2026-05-01T1{i}:00:00")
    ids = [r["id"] for r in client.get("/api/bio-sensor/scan-history").json()["data"]]
    res = client.get(f"/api/bio-sensor/scan-history?before_id={ids[0]}&limit=2")
    assert [r["id"] for r in res.json()["data"]] == ids[1:3]


def test_scan_history_before_id_past_oldest_returns_empty(client_with_db):
    client, db = client_with_db
    _insert(db, task_id="T1", location_id="B_101-1", bed_name="101-1",
            timestamp="2026-05-01T10:00:00")
    oldest = client.get("/api/bio-sensor/scan-history").json()["data"][-1]
    res = client.get(f"/api/bio-sensor/scan-history?before_id={oldest['id']}")
    assert res.json()["count"] == 0
    assert res.json()["data"] == []
