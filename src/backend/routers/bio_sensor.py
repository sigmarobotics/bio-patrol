from fastapi import APIRouter
from dependencies import get_bio_sensor_client

router = APIRouter(prefix='/api/bio-sensor', tags=['Bio Sensor'])


@router.get("/scan-history")
async def get_bio_sensor_scan_history(limit: int = 100, task_id: str = None, location_id: str = None):
    """Get historical bio-sensor scan data from database.

    location_id is the canonical join key, not bed_name which is free-text.
    """
    client = get_bio_sensor_client()
    if client is None:
        return {"status": "disabled", "message": "Bio-sensor MQTT is disabled"}
    import sqlite3

    conn = None
    try:
        conn = sqlite3.connect(client.db_path)
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
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""

        params.append(limit)
        cursor.execute(
            f"""
            SELECT id, task_id, location_id, bed_name, timestamp, retry_count,
                   status, bpm, rpm, is_valid, data_json, details
            FROM sensor_scan_data
            {where}
            ORDER BY timestamp DESC, retry_count ASC
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

@router.get("/latest-by-bed")
async def get_latest_by_bed():
    """Return one row per bed (location_id) — the latest scan record."""
    client = get_bio_sensor_client()
    if client is None:
        return {"status": "disabled", "data": [], "count": 0}
    import sqlite3

    conn = None
    try:
        conn = sqlite3.connect(client.db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT id, task_id, location_id, bed_name, timestamp, retry_count,
                   status, bpm, rpm, is_valid, data_json, details
            FROM (
                SELECT *,
                       ROW_NUMBER() OVER (PARTITION BY location_id ORDER BY timestamp DESC) AS rn
                FROM sensor_scan_data
                WHERE location_id IS NOT NULL
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
