"""IT-15: GET /api/bio-sensor/bed-stats unit tests."""
from tests.unit.test_latest_by_bed import client_with_db, _insert  # noqa: F401  (reuse fixtures)


def _run(db, task_id, *, valid=True, bpm=70, rpm=16, ts="2026-05-01T10:00:00", retries=0):
    """One patrol run: `retries` failed rows, then the outcome row."""
    for i in range(retries):
        _insert(db, task_id=task_id, location_id="B_101-1", bed_name="101-1",
                timestamp=ts, retry_count=i, status=2, bpm=0, rpm=0, is_valid=False)
    _insert(db, task_id=task_id, location_id="B_101-1", bed_name="101-1",
            timestamp=ts, retry_count=retries, status=4 if valid else 2,
            bpm=bpm, rpm=rpm, is_valid=valid)


def test_bed_stats_averages_exclude_invalid_runs(client_with_db):
    client, db = client_with_db
    _run(db, "T1", valid=True, bpm=60, rpm=10, ts="2026-05-01T10:00:00")
    _run(db, "T2", valid=False, bpm=0, rpm=0, ts="2026-05-01T11:00:00")
    _run(db, "T3", valid=True, bpm=80, rpm=20, ts="2026-05-01T12:00:00")
    stats = client.get("/api/bio-sensor/bed-stats?location_id=B_101-1").json()["stats"]
    assert stats["avg_bpm"] == 70.0
    assert stats["avg_rpm"] == 15.0
    assert stats["valid_count"] == 2


def test_bed_stats_success_rate_counts_runs_not_retry_rows(client_with_db):
    client, db = client_with_db
    _run(db, "T1", valid=True, ts="2026-05-01T10:00:00")
    _run(db, "T2", valid=False, ts="2026-05-01T11:00:00", retries=20)  # 21 rows, one run
    res = client.get("/api/bio-sensor/bed-stats?location_id=B_101-1").json()
    assert res["stats"]["success_rate"] == 0.5
    assert res["stats"]["valid_count"] == 1


def test_bed_stats_empty_bed_returns_nulls_and_empty_trend(client_with_db):
    client, _ = client_with_db
    res = client.get("/api/bio-sensor/bed-stats?location_id=B_nothing").json()
    assert res["status"] == "success"
    assert res["stats"] == {"avg_bpm": None, "avg_rpm": None, "valid_count": 0,
                            "success_rate": None, "window": 30}
    assert res["trend"] == []


def test_bed_stats_all_failed_runs_returns_null_averages(client_with_db):
    client, db = client_with_db
    _run(db, "T1", valid=False, ts="2026-05-01T10:00:00", retries=3)
    _run(db, "T2", valid=False, ts="2026-05-01T11:00:00", retries=3)
    res = client.get("/api/bio-sensor/bed-stats?location_id=B_101-1").json()
    assert res["stats"]["avg_bpm"] is None
    assert res["stats"]["success_rate"] == 0.0
    assert res["trend"] == []


def test_bed_stats_uses_only_newest_window_valid_runs(client_with_db):
    client, db = client_with_db
    # 5 old runs at bpm=100, then 30 newer ones at bpm=60 — window=30 must not
    # see the old ones.
    for i in range(5):
        _run(db, f"OLD{i}", bpm=100, rpm=30, ts=f"2026-05-01T{i:02d}:00:00")
    for i in range(30):
        _run(db, f"NEW{i}", bpm=60, rpm=10, ts=f"2026-05-02T{i % 24:02d}:00:00")
    stats = client.get("/api/bio-sensor/bed-stats?location_id=B_101-1").json()["stats"]
    assert stats["avg_bpm"] == 60.0
    assert stats["valid_count"] == 30


def test_bed_stats_window_param_changes_result(client_with_db):
    client, db = client_with_db
    for i in range(20):
        _run(db, f"OLD{i}", bpm=100, rpm=30, ts=f"2026-05-01T{i % 24:02d}:00:00")
    for i in range(10):
        _run(db, f"NEW{i}", bpm=60, rpm=10, ts=f"2026-05-02T{i:02d}:00:00")
    w10 = client.get("/api/bio-sensor/bed-stats?location_id=B_101-1&window=10").json()["stats"]
    w30 = client.get("/api/bio-sensor/bed-stats?location_id=B_101-1&window=30").json()["stats"]
    assert w10["avg_bpm"] == 60.0 and w10["window"] == 10 and w10["valid_count"] == 10
    assert w30["avg_bpm"] == 86.7 and w30["window"] == 30 and w30["valid_count"] == 30


def test_bed_stats_window_clamped_to_1_and_200(client_with_db):
    client, db = client_with_db
    _run(db, "T1", bpm=60, ts="2026-05-01T10:00:00")
    _run(db, "T2", bpm=80, ts="2026-05-01T11:00:00")
    low = client.get("/api/bio-sensor/bed-stats?location_id=B_101-1&window=0").json()["stats"]
    assert low["window"] == 1
    assert low["avg_bpm"] == 80.0  # newest run only
    high = client.get("/api/bio-sensor/bed-stats?location_id=B_101-1&window=9999").json()["stats"]
    assert high["window"] == 200


def test_bed_stats_trend_is_chronological_valid_runs(client_with_db):
    client, db = client_with_db
    _run(db, "T1", bpm=60, rpm=10, ts="2026-05-01T10:00:00")
    _run(db, "T2", valid=False, ts="2026-05-01T11:00:00")
    _run(db, "T3", bpm=80, rpm=20, ts="2026-05-01T12:00:00")
    trend = client.get("/api/bio-sensor/bed-stats?location_id=B_101-1").json()["trend"]
    assert [p["bpm"] for p in trend] == [60, 80]
    assert [p["timestamp"] for p in trend] == ["2026-05-01T10:00:00", "2026-05-01T12:00:00"]


def test_bed_stats_scoped_to_location(client_with_db):
    client, db = client_with_db
    _run(db, "T1", bpm=60, ts="2026-05-01T10:00:00")
    _insert(db, task_id="T2", location_id="B_101-2", bed_name="101-2",
            timestamp="2026-05-01T11:00:00", bpm=200, rpm=40)
    stats = client.get("/api/bio-sensor/bed-stats?location_id=B_101-1").json()["stats"]
    assert stats["avg_bpm"] == 60.0
    assert stats["valid_count"] == 1


def test_bed_stats_bed_name_filter_separates_shared_location(client_with_db):
    client, db = client_with_db
    # Two beds share one Kachaka destination AND one patrol task_id — without
    # the bed_name filter, one bed's stats absorb the roommate's readings.
    _insert(db, task_id="T1", location_id="辦公室222", bed_name="102-3",
            timestamp="2026-05-01T10:00:00", bpm=60, rpm=10)
    _insert(db, task_id="T1", location_id="辦公室222", bed_name="103-3",
            timestamp="2026-05-01T10:05:00", bpm=90, rpm=20)
    a = client.get("/api/bio-sensor/bed-stats",
                   params={"location_id": "辦公室222", "bed_name": "102-3"}).json()["stats"]
    b = client.get("/api/bio-sensor/bed-stats",
                   params={"location_id": "辦公室222", "bed_name": "103-3"}).json()["stats"]
    assert a["avg_bpm"] == 60.0 and a["valid_count"] == 1
    assert b["avg_bpm"] == 90.0 and b["valid_count"] == 1
