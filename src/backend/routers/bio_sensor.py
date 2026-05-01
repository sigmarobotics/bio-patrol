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

@router.get("/latest")
async def get_latest_bio_sensor_data():
    """Get the latest bio-sensor data received via MQTT."""
    try:
        client = get_bio_sensor_client()
        if client is None:
            return {"status": "disabled", "message": "Bio-sensor MQTT is disabled"}
        if client.latest_data is None:
            return {"status": "no_data", "message": "No sensor data received yet"}
        return {"status": "success", "data": client.latest_data}
    except Exception as e:
        return {"status": "error", "message": str(e)}

@router.get("/scan-history")
async def get_bio_sensor_scan_history(limit: int = 100, task_id: str = None):
    """Get historical bio-sensor scan data from database."""
    try:
        client = get_bio_sensor_client()
        if client is None:
            return {"status": "disabled", "message": "Bio-sensor MQTT is disabled"}
        import sqlite3

        conn = sqlite3.connect(client.db_path)
        cursor = conn.cursor()

        if task_id:
            search_pattern = f'{task_id}%'
            cursor.execute('''
                SELECT id, task_id, location_id, bed_name, timestamp, retry_count, status, bpm, rpm, is_valid, data_json, details
                FROM sensor_scan_data
                WHERE task_id LIKE ?
                ORDER BY timestamp DESC, retry_count ASC
                LIMIT ?
            ''', (search_pattern, limit))
        else:
            cursor.execute('''
                SELECT id, task_id, location_id, bed_name, timestamp, retry_count, status, bpm, rpm, is_valid, data_json, details
                FROM sensor_scan_data
                ORDER BY timestamp DESC, retry_count ASC
                LIMIT ?
            ''', (limit,))

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
