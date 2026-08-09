from fastapi import APIRouter
from dependencies import get_bio_sensor_client
from utils.sqlite_wal import connect_db

router = APIRouter(prefix='/api/bio-sensor', tags=['Bio Sensor'])


@router.get("/scan-history")
async def get_bio_sensor_scan_history(limit: int = 100, task_id: str = None, location_id: str = None,
                                      before_id: int = None):
    """Get historical bio-sensor scan data from database.

    location_id is the canonical join key, not bed_name which is free-text.
    before_id is the pagination cursor; rows sort by id DESC so the cursor
    and the sort always agree (timestamps are wall-clock strings that can go
    backwards on tz changes or NTP steps — id is true insert order).
    """
    client = get_bio_sensor_client()
    if client is None:
        return {"status": "disabled", "message": "Bio-sensor MQTT is disabled"}
    import sqlite3

    conn = None
    try:
        conn = connect_db(client.db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        clauses = []
        params = []
        if task_id:
            clauses.append("task_id LIKE ?")
            params.append(f"{task_id}%")
        if location_id:
            clauses.append("location_id = ?")
            params.append(location_id)
        if before_id:
            clauses.append("id < ?")
            params.append(before_id)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""

        params.append(limit)
        cursor.execute(
            f"""
            SELECT id, task_id, location_id, bed_name, timestamp, retry_count,
                   status, bpm, rpm, is_valid, data_json, details
            FROM sensor_scan_data
            {where}
            ORDER BY id DESC
            LIMIT ?
            """,
            params,
        )
        rows = cursor.fetchall()
        data = [{**dict(r), "is_valid": bool(r["is_valid"])} for r in rows]
        return {"status": "success", "data": data, "count": len(data)}
    except Exception as e:
        return {"status": "error", "message": str(e)}
    finally:
        if conn is not None:
            conn.close()

@router.get("/bed-stats")
async def get_bed_stats(location_id: str, window: int = 30, bed_name: str = None):
    """Rolling stats + trend for one bed over its most recent runs.

    A failed scan writes one row per retry, so rows are collapsed into runs by
    task_id first — success_rate counts runs, not retries. Averages come from
    the newest `window` VALID runs only; success_rate from the newest `window`
    runs including failures. Pass bed_name too when beds share a Kachaka
    destination — one patrol stamps the same task_id for both beds, and
    location_id alone would merge the roommates' readings into one run.
    """
    client = get_bio_sensor_client()
    if client is None:
        return {"status": "disabled", "message": "Bio-sensor MQTT is disabled"}
    import sqlite3

    window = max(1, min(window, 200))
    conn = None
    try:
        conn = connect_db(client.db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        clauses = ["location_id = ?"]
        params = [location_id]
        if bed_name:
            clauses.append("bed_name = ?")
            params.append(bed_name)
        cursor.execute(
            f"""
            SELECT task_id, timestamp, bpm, rpm, is_valid
            FROM sensor_scan_data
            WHERE {' AND '.join(clauses)}
            ORDER BY id DESC
            LIMIT 2000
            """,
            params,
        )

        runs = []      # newest-first, one entry per task_id
        by_task = {}
        for r in cursor.fetchall():
            run = by_task.get(r["task_id"])
            if run is None:
                run = {"is_valid": False, "timestamp": r["timestamp"], "bpm": None, "rpm": None}
                by_task[r["task_id"]] = run
                runs.append(run)
            if r["is_valid"] and not run["is_valid"]:
                run.update(is_valid=True, timestamp=r["timestamp"], bpm=r["bpm"], rpm=r["rpm"])

        recent = runs[:window]
        valid = [r for r in runs if r["is_valid"]][:window]
        stats = {
            "avg_bpm": round(sum(r["bpm"] for r in valid) / len(valid), 1) if valid else None,
            "avg_rpm": round(sum(r["rpm"] for r in valid) / len(valid), 1) if valid else None,
            "valid_count": len(valid),
            "success_rate": (
                round(sum(1 for r in recent if r["is_valid"]) / len(recent), 3) if recent else None
            ),
            "window": window,
        }
        trend = [
            {"timestamp": r["timestamp"], "bpm": r["bpm"], "rpm": r["rpm"]}
            for r in reversed(valid)
        ]
        return {"status": "success", "stats": stats, "trend": trend}
    except Exception as e:
        return {"status": "error", "message": str(e)}
    finally:
        if conn is not None:
            conn.close()

@router.get("/latest-by-bed")
async def get_latest_by_bed():
    """Return one row per bed (bed_name) — the latest scan record.

    Partitions by bed_name, not location_id: multiple beds can share a
    Kachaka destination (e.g. two beds at one drop-point), and the
    dashboard's per-bed-card semantics require per-bed_name freshness.
    """
    client = get_bio_sensor_client()
    if client is None:
        return {"status": "disabled", "data": [], "count": 0}
    import sqlite3

    conn = None
    try:
        conn = connect_db(client.db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT id, task_id, location_id, bed_name, timestamp, retry_count,
                   status, bpm, rpm, is_valid, data_json, details
            FROM (
                SELECT *,
                       ROW_NUMBER() OVER (PARTITION BY bed_name ORDER BY timestamp DESC) AS rn
                FROM sensor_scan_data
                WHERE bed_name IS NOT NULL
            )
            WHERE rn = 1
            """
        )
        rows = cursor.fetchall()
        data = [{**dict(r), "is_valid": bool(r["is_valid"])} for r in rows]
        return {"status": "success", "data": data, "count": len(data)}
    except Exception as e:
        return {"status": "error", "message": str(e)}
    finally:
        if conn is not None:
            conn.close()

@router.get("/scan")
async def get_bio_sensor_scan_data():
    """Execute a bio-sensor scan task and return all collected data."""
    try:
        client = get_bio_sensor_client()
        if client is None:
            return {"status": "disabled", "message": "Bio-sensor MQTT is disabled"}
        # Task-less scans default to location_id='manual' inside
        # get_valid_scan_data; bed_name stays None so ad-hoc rows never
        # show up in /latest-by-bed.
        outcome = await client.get_valid_scan_data()

        if outcome.valid_record is None:
            return {
                "status": "no_valid_data",
                "message": "No valid scan data received after all retries",
                "task_id": outcome.task_id,
                "details": outcome.last_failure_reason,
                "retry_count": outcome.retry_count,
            }
        return {
            "status": "success",
            "task_id": outcome.task_id,
            "data": outcome.valid_record,
        }
    except Exception as e:
        return {"status": "error", "message": str(e)}

@router.post("/generate-fake-data")
async def generate_fake_sensor_data(num_tasks: int = 10):
    """Generate fake sensor scan data for testing purposes."""
    try:
        from utils.generate_fake_sensor_data import generate_fake_scan_tasks, get_db_path, init_database

        db_path = get_db_path()
        init_database(db_path)
        generate_fake_scan_tasks(db_path, num_tasks)

        return {
            "status": "success",
            "message": f"Generated {num_tasks} fake scan tasks",
            "db_path": db_path
        }
    except Exception as e:
        return {"status": "error", "message": str(e)}
