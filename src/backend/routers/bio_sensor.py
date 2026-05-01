from fastapi import APIRouter
from dependencies import get_bio_sensor_client

router = APIRouter(prefix='/api/bio-sensor', tags=['Bio Sensor'])


def _row_to_dict(row):
    """Convert a sensor_scan_data SELECT row to a dict (IT-9: shared by /scan-history + /latest-by-bed)."""
    return {
        "id": row[0],
        "task_id": row[1],
        "location_id": row[2],
        "bed_name": row[3],
        "timestamp": row[4],
        "retry_count": row[5],
        "status": row[6],
        "bpm": row[7],
        "rpm": row[8],
        "is_valid": bool(row[9]),
        "data_json": row[10],
        "details": row[11],
    }

@router.get("/scan-history")
async def get_bio_sensor_scan_history(limit: int = 100, task_id: str = None, location_id: str = None):
    """Get historical bio-sensor scan data from database.

    IT-9: location_id filter added (canonical join key, not bed_name which is free-text).
    """
    try:
        client = get_bio_sensor_client()
        if client is None:
            return {"status": "disabled", "message": "Bio-sensor MQTT is disabled"}
        import sqlite3

        conn = sqlite3.connect(client.db_path)
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
        conn.close()

        data = [_row_to_dict(row) for row in rows]
        return {"status": "success", "data": data, "count": len(data)}
    except Exception as e:
        return {"status": "error", "message": str(e)}

@router.get("/latest-by-bed")
async def get_latest_by_bed():
    """IT-9: Return one row per bed (location_id) — the latest scan record."""
    try:
        client = get_bio_sensor_client()
        if client is None:
            return {"status": "disabled", "data": [], "count": 0}
        import sqlite3

        conn = sqlite3.connect(client.db_path)
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
        conn.close()
        data = [_row_to_dict(row) for row in rows]
        return {"status": "success", "data": data, "count": len(data)}
    except Exception as e:
        return {"status": "error", "message": str(e)}

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
