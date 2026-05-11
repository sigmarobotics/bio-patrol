"""
MQTT client for receiving physiological sensor data.
"""
import json
import paho.mqtt.client as mqtt
import logging
import os
import asyncio
import sqlite3
from common_types import get_now

logger = logging.getLogger("BioSensorMQTTClient")


def is_valid_scan(record: dict, valid_status: int) -> bool:
    """Return True when a sensor record is a usable bio-scan reading.

    A reading is valid only when status matches the configured `valid_status`
    and both bpm/rpm are positive. Treats missing fields as invalid.
    """
    return (
        record.get("status") == valid_status
        and (record.get("bpm") or 0) > 0
        and (record.get("rpm") or 0) > 0
    )


class BioSensorMQTTClient:
    def __init__(self, broker="localhost", port=1803, topic="/my/default/channel", db_path=None):
        self.broker = broker
        self.port = port
        self.topic = topic
        if db_path is None:
            # From src/backend/services/bio_sensor_mqtt.py → up 4 levels to project root
            project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
            data_dir = os.path.join(project_root, "data")
            os.makedirs(data_dir, exist_ok=True)
            self.db_path = os.path.join(data_dir, "sensor_data.db")
        else:
            self.db_path = db_path

        self.client = mqtt.Client(protocol=mqtt.MQTTv31)
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.on_message = self._on_message
        # paho's background loop handles reconnection only when these are set
        # BEFORE loop_start(). Without this, a startup-time broker outage
        # leaves the singleton dead forever — see sensor.log 2026-05-04+.
        self.client.reconnect_delay_set(min_delay=1, max_delay=30)
        self.latest_data = None
        self.connected = False
        self._init_database()
    
    def _init_database(self):
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS sensor_scan_data (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT NOT NULL,
                location_id TEXT NOT NULL,
                bed_name TEXT NULL,
                timestamp TEXT NOT NULL,
                retry_count INTEGER NOT NULL,
                status INTEGER,
                bpm INTEGER,
                rpm INTEGER,
                data_json TEXT,
                is_valid BOOLEAN DEFAULT FALSE,
                details TEXT NULL
            )
        ''')
        # Migration: add bed_name column if missing (existing DB)
        try:
            cursor.execute("ALTER TABLE sensor_scan_data ADD COLUMN bed_name TEXT NULL")
            conn.commit()
        except sqlite3.OperationalError:
            pass  # column already exists
        # Migration: rename bed_id → location_id (existing DB)
        try:
            cursor.execute("ALTER TABLE sensor_scan_data RENAME COLUMN bed_id TO location_id")
            conn.commit()
        except sqlite3.OperationalError:
            pass  # column already renamed or doesn't exist
        # Index supports the dashboard's per-bed latest lookup
        # (PARTITION BY location_id ORDER BY timestamp DESC).
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_scan_loc_ts ON sensor_scan_data(location_id, timestamp DESC)"
        )
        conn.commit()
        conn.close()
    
    def _on_connect(self, client, userdata, flags, rc):
        if rc == 0:
            self.connected = True
            result, mid = client.subscribe(self.topic)
            logger.info(f"Connected to MQTT broker, subscribed to {self.topic}, result={result}, mid={mid}")
        else:
            self.connected = False
            logger.error(f"MQTT connection failed with code {rc}")

    def _on_disconnect(self, client, userdata, rc):
        self.connected = False
        if rc != 0:
            logger.warning(f"MQTT broker disconnected unexpectedly (rc={rc})")
    
    def _on_message(self, client, userdata, msg):
        # logger.info(f"Received message: {msg.topic} {msg.payload.decode()}")
        self.latest_data = json.loads(msg.payload.decode())
    
    def stop(self):
        self.client.loop_stop()
        self.client.disconnect()
    
    def start(self):
        # connect_async + loop_start lets paho's background thread own
        # retries — a broker that's down at boot will keep retrying with the
        # configured reconnect_delay_set backoff instead of leaving a dead
        # singleton that never recovers.
        try:
            self.client.connect_async(self.broker, self.port, 60)
        except Exception as e:
            # connect_async only validates the host string; a raise here is a
            # config bug, not a network outage. Loop_start still runs so a
            # later config-fix-and-reload can pick up.
            logger.error(f"MQTT connect_async rejected {self.broker}:{self.port}: {e}")
        self.client.loop_start()

    def _save_scan_data(self, task_id, data, retry_count, is_valid=False):
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        timestamp = get_now().isoformat()
        cursor.execute('''
            INSERT INTO sensor_scan_data
            (task_id, location_id, bed_name, timestamp, retry_count, status, bpm, rpm, data_json, is_valid, details)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            task_id,
            data.get('location_id'),
            data.get('bed_name'),
            timestamp,
            retry_count,
            data.get('status'),
            data.get('bpm'),
            data.get('rpm'),
            json.dumps(data),
            is_valid,
            data.get('details'),
        ))
        conn.commit()
        conn.close()

    async def get_valid_scan_data(self, task_id=None, target_bed=None, bed_name=None):
        """Run the bio-scan window and return a ScanOutcome.

        Replaces the legacy ``{"task_id", "data"}`` return. Callers read
        ``outcome.valid_record`` (None when retries exhausted).
        """
        from services.notifications.evaluator import ScanOutcome

        if task_id is None:
            task_id = get_now().strftime("%Y%m%d%H%M%S")

        if not self.connected:
            # paho's loop is already retrying in the background; nudge it once
            # so a long-idle singleton doesn't have to wait out the full
            # reconnect_delay backoff window before this scan starts.
            logger.warning("MQTT broker not connected at scan start; forcing reconnect")
            try:
                self.client.reconnect()
            except Exception as e:
                logger.warning(f"reconnect() raised (paho loop will keep retrying): {e}")

        try:
            from settings.config import get_runtime_settings
            cfg = get_runtime_settings()
        except Exception:
            cfg = {}

        WAIT_TIME = cfg.get("bio_scan_wait_time", 10)
        RETRY_COUNT = cfg.get("bio_scan_retry_count", 19)
        INT_WAIT_TIME = cfg.get("bio_scan_initial_wait", 120)
        VALID_STATUS = cfg.get("bio_scan_valid_status", 4)

        valid_data: dict | None = None
        has_any_data = False
        last_record_processed: dict | None = None
        retry_count = 0  # loop variable; pre-init covers the RETRY_COUNT == 0 edge

        await asyncio.sleep(INT_WAIT_TIME)
        for retry_count in range(RETRY_COUNT):
            if self.latest_data and 'records' in self.latest_data:
                has_any_data = True
                for data in self.latest_data['records']:
                    logger.debug("scan_data: %s", data)
                    is_valid = is_valid_scan(data, VALID_STATUS)
                    data['details'] = '量測正常' if is_valid else '無有效量測數値'
                    data['location_id'] = target_bed
                    data['bed_name'] = bed_name
                    self._save_scan_data(task_id, data, retry_count, is_valid)
                    last_record_processed = data
                    if is_valid and valid_data is None:
                        valid_data = data
                if valid_data is not None:
                    return ScanOutcome(
                        task_id=task_id,
                        location_id=target_bed,
                        bed_name=bed_name,
                        valid_record=valid_data,
                        retry_count=retry_count,
                        last_record_raw=last_record_processed,
                        last_failure_reason=None,
                    )

            if retry_count + 1 < RETRY_COUNT:
                await asyncio.sleep(WAIT_TIME)

        # All retries exhausted without a valid record.
        if not has_any_data:
            no_data = {
                "location_id": target_bed,
                "bed_name": bed_name,
                "status": None,
                "bpm": None,
                "rpm": None,
                "details": "未收到感測器資料（MQTT無連線或無數據）",
            }
            self._save_scan_data(task_id, no_data, RETRY_COUNT, is_valid=False)
            return ScanOutcome(
                task_id=task_id,
                location_id=target_bed,
                bed_name=bed_name,
                valid_record=None,
                retry_count=retry_count,
                last_record_raw=None,
                last_failure_reason="未收到感測器資料（MQTT無連線或無數據）",
            )

        # has_any_data but no valid hit — use the last processed record's details.
        return ScanOutcome(
            task_id=task_id,
            location_id=target_bed,
            bed_name=bed_name,
            valid_record=None,
            retry_count=retry_count,
            last_record_raw=last_record_processed,
            last_failure_reason=last_record_processed.get('details') if last_record_processed else "無有效量測數値",
        )


