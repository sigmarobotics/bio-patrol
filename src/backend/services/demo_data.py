"""IT-21 demo synthetic data: the fixed demo script, synthetic Wisleep-shaped
readings, and the separate demo_data.db they live in.

Synthetic readings go through the real evaluators and dispatcher, but they are
stored ONLY here — never in sensor_data.db, so the dashboard and history views
can never show a demo value as a resident's reading (CORNER-063).

DB_PATH is read at call time (``_connect``), so tests can point it at a temp
file with ``monkeypatch.setattr(demo_data, "DB_PATH", ...)``.
"""
from __future__ import annotations

import json
import os
import random
import sqlite3
from datetime import timedelta
from typing import Iterable

from common_types import get_now
from services.bio_sensor_mqtt import is_valid_scan
from services.notifications.evaluator import vitals_out_of_band
from services.notifications.events import AnomalyEvent, display_title
from utils.sqlite_wal import connect_db

# From src/backend/services/demo_data.py → up 4 levels to project root
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
DB_PATH = os.path.join(_PROJECT_ROOT, "data", "demo_data.db")

# Route order → scenario, cycling: normal → vitals abnormal → person absent.
SCENARIOS = ("normal", "abnormal", "absent")

# task_id of the synthetic 30-day history rows (never a real run id).
HISTORY_TASK_ID = "demo-history"
HISTORY_DAYS = 30

_initialised: set[str] = set()


def _connect() -> sqlite3.Connection:
    path = DB_PATH
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = connect_db(path)
    conn.row_factory = sqlite3.Row
    if path not in _initialised or not _has_tables(conn):
        conn.execute('''
            CREATE TABLE IF NOT EXISTS demo_scan_data (
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
                details TEXT NULL,
                scenario TEXT NULL
            )
        ''')
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_demo_scan_bed_ts ON demo_scan_data(bed_name, timestamp)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_demo_scan_task ON demo_scan_data(task_id)"
        )
        conn.execute('''
            CREATE TABLE IF NOT EXISTS demo_notifications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT NULL,
                timestamp TEXT NOT NULL,
                severity TEXT NOT NULL,
                source TEXT NOT NULL,
                bed_key TEXT NULL,
                title TEXT NOT NULL,
                body TEXT NOT NULL
            )
        ''')
        conn.commit()
        _initialised.add(path)
    return conn


def _has_tables(conn: sqlite3.Connection) -> bool:
    n = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' "
        "AND name IN ('demo_scan_data', 'demo_notifications')"
    ).fetchone()[0]
    return n == 2


# ── Script + synthesis ───────────────────────────────────────────────────────

def scenario_for(index: int) -> str:
    """Scenario of the index-th seat on the demo route (CORNER-066)."""
    return SCENARIOS[index % len(SCENARIOS)]


def synth_record(scenario: str, cfg: dict, rng: random.Random) -> dict:
    """A Wisleep-shaped record ({status, bpm, rpm}) for one scenario.

    abnormal is derived from the thresholds in force, so it crosses them
    however the site configures them.
    """
    if scenario == "absent":
        return {"status": 0, "bpm": 0, "rpm": 0}
    status = cfg.get("bio_scan_valid_status", 4)
    if scenario == "abnormal":
        return {
            "status": status,
            "bpm": cfg.get("vitals_hr_high", 120) + rng.randint(8, 15),
            "rpm": cfg.get("vitals_rr_high", 30) + rng.randint(2, 5),
        }
    return {"status": status, "bpm": rng.randint(68, 88), "rpm": rng.randint(14, 20)}


def save_scan(task_id: str, bed_key: str, location_id: str, scenario: str,
              record: dict, cfg: dict, timestamp: str | None = None) -> tuple[dict, bool]:
    """Store one synthetic reading; returns (row data, is_valid) with the same
    validity rule and details wording as the real scan."""
    valid = is_valid_scan(record, cfg.get("bio_scan_valid_status", 4))
    data = {
        **record,
        "details": "量測正常" if valid else "無有效量測數値",
        "location_id": location_id,
        "bed_name": bed_key,
        "scenario": scenario,
    }
    conn = _connect()
    conn.execute('''
        INSERT INTO demo_scan_data
        (task_id, location_id, bed_name, timestamp, retry_count, status, bpm, rpm,
         data_json, is_valid, details, scenario)
        VALUES (?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?)
    ''', (
        task_id, location_id, bed_key, timestamp or get_now().isoformat(),
        data["status"], data["bpm"], data["rpm"], json.dumps(data, ensure_ascii=False),
        valid, data["details"], scenario,
    ))
    conn.commit()
    conn.close()
    return data, valid


def ensure_history(bed_keys: Iterable[str], cfg: dict | None = None) -> None:
    """Back-fill one normal reading per seat per day for the last 30 days
    (ARCH-034). Today's point is the demo run itself, so the abnormal seat's
    history is normal except today and the absent seat's history is normal.
    Fixed seed per (seat, day) and skip-if-present → idempotent."""
    if cfg is None:
        from settings.config import get_runtime_settings
        cfg = get_runtime_settings()
    base = get_now().replace(hour=10, minute=0, second=0, microsecond=0)
    conn = _connect()
    for bed_key in bed_keys:
        have = {
            r[0] for r in conn.execute(
                "SELECT substr(timestamp, 1, 10) FROM demo_scan_data "
                "WHERE task_id = ? AND bed_name = ?",
                (HISTORY_TASK_ID, bed_key),
            )
        }
        for days_ago in range(HISTORY_DAYS, 0, -1):
            ts = base - timedelta(days=days_ago)
            day = ts.date().isoformat()
            if day in have:
                continue
            rec = synth_record("normal", cfg, random.Random(f"{bed_key}:{day}"))
            conn.execute('''
                INSERT INTO demo_scan_data
                (task_id, location_id, bed_name, timestamp, retry_count, status, bpm, rpm,
                 data_json, is_valid, details, scenario)
                VALUES (?, ?, ?, ?, 0, ?, ?, ?, ?, 1, '量測正常', 'history')
            ''', (
                HISTORY_TASK_ID, "", bed_key, ts.isoformat(),
                rec["status"], rec["bpm"], rec["rpm"], json.dumps(rec),
            ))
    conn.commit()
    conn.close()


def seat_state(row: dict, cfg: dict) -> str:
    """normal / abnormal / absent for one stored demo reading — the same rules
    the evaluators apply (invalid → 偵測不到人, valid out of band → alert)."""
    if not row.get("is_valid"):
        return "absent"
    if vitals_out_of_band(row.get("bpm") or 0, row.get("rpm") or 0, cfg):
        return "abnormal"
    return "normal"


# ── Notifications preview ────────────────────────────────────────────────────

def save_notification(event: AnomalyEvent) -> None:
    conn = _connect()
    conn.execute('''
        INSERT INTO demo_notifications (task_id, timestamp, severity, source, bed_key, title, body)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    ''', (
        event.task_id, event.timestamp.isoformat(), event.severity.value,
        event.source.value, event.bed_key, display_title(event), event.body,
    ))
    conn.commit()
    conn.close()


# ── Reads (for /api/demo/* and the run summary) ─────────────────────────────

def scan_rows(task_id: str) -> list[dict]:
    conn = _connect()
    rows = conn.execute(
        "SELECT * FROM demo_scan_data WHERE task_id = ? ORDER BY id", (task_id,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def latest_run_task_id() -> str | None:
    conn = _connect()
    row = conn.execute(
        "SELECT task_id FROM demo_scan_data WHERE task_id != ? ORDER BY id DESC LIMIT 1",
        (HISTORY_TASK_ID,),
    ).fetchone()
    conn.close()
    return row[0] if row else None


def history_rows(bed_key: str, days: int = HISTORY_DAYS) -> list[dict]:
    midnight = get_now().replace(hour=0, minute=0, second=0, microsecond=0)
    since = (midnight - timedelta(days=days)).isoformat()
    conn = _connect()
    rows = conn.execute(
        "SELECT task_id, timestamp, status, bpm, rpm, is_valid, scenario FROM demo_scan_data "
        "WHERE bed_name = ? AND timestamp >= ? ORDER BY timestamp",
        (bed_key, since),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def notification_rows(task_id: str | None) -> tuple[str | None, list[dict]]:
    """Preview items for one run; ``task_id=None`` → the most recent run."""
    conn = _connect()
    if task_id is None:
        row = conn.execute(
            "SELECT task_id FROM demo_notifications ORDER BY id DESC LIMIT 1"
        ).fetchone()
        task_id = row[0] if row else None
    rows = conn.execute(
        "SELECT timestamp, severity, source, bed_key, title, body FROM demo_notifications "
        "WHERE task_id = ? ORDER BY id",
        (task_id,),
    ).fetchall() if task_id else []
    conn.close()
    return task_id, [dict(r) for r in rows]
