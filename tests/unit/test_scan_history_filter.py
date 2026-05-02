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
