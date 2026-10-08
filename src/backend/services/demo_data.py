"""IT-21 demo synthetic data: the fixed demo script, synthetic Wisleep-shaped
readings, and the separate demo_data.db they live in.

Synthetic readings go through the real evaluators and dispatcher, but they are
stored ONLY here — never in sensor_data.db, so the dashboard and history views
can never show a demo value as a resident's reading (CORNER-063). The /demo
view is the same SPA reading this DB instead (IT-21b, ``X-Bio-Data: demo``).

DB_PATH is read at call time (``_connect``), so tests can point it at a temp
file with ``monkeypatch.setattr(demo_data, "DB_PATH", ...)``.
"""
from __future__ import annotations

import hashlib
import json
import os
import random
import sqlite3
from datetime import timedelta
from typing import Iterable

from common_types import get_now
from services.bio_sensor_mqtt import is_valid_scan
from utils.sqlite_wal import connect_db

# From src/backend/services/demo_data.py → up 4 levels to project root
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
DB_PATH = os.path.join(_PROJECT_ROOT, "data", "demo_data.db")

# Route order → scenario, cycling: normal → vitals abnormal → person absent.
SCENARIOS = ("normal", "abnormal", "absent")

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
        # IT-21 stored the whole synthetic history under one shared task_id,
        # which the history tab would list as a single giant run. It is
        # synthetic — drop it; the next demo start back-fills per-day runs.
        conn.execute("DELETE FROM demo_scan_data WHERE task_id = 'demo-history'")
        conn.commit()
        _initialised.add(path)
    return conn


def _has_tables(conn: sqlite3.Connection) -> bool:
    n = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name = 'demo_scan_data'"
    ).fetchone()[0]
    return n == 1


def db_path() -> str:
    """Path of the demo DB, with its table guaranteed to exist — an empty demo
    DB reads as no rows, not as an error."""
    _connect().close()
    return DB_PATH


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


def history_task_id(ts) -> str:
    """Real-format run id (YYYYMMDDHHMMSS-xxxxxx) for one synthetic history
    day, stable per day, so the history tab lists each day as its own run."""
    suffix = hashlib.sha1(f"demo-history:{ts.date().isoformat()}".encode()).hexdigest()[:6]
    return f"{ts.strftime('%Y%m%d%H%M%S')}-{suffix}"


def ensure_history(beds: Iterable[dict], cfg: dict | None = None) -> None:
    """Back-fill one normal reading per seat per day for the last 30 days
    (ARCH-034). ``beds``: {bed_key, location_id} per seat. Today's point is the
    demo run itself, so the abnormal seat's history is normal except today and
    the absent seat's history is normal. Each day is one run at 10:00 across
    every seat. Fixed seed per (seat, day) and skip-if-present → idempotent."""
    if cfg is None:
        from settings.config import get_runtime_settings
        cfg = get_runtime_settings()
    base = get_now().replace(hour=10, minute=0, second=0, microsecond=0)
    conn = _connect()
    beds = list(beds)
    have = {
        bed["bed_key"]: {
            r[0] for r in conn.execute(
                "SELECT substr(timestamp, 1, 10) FROM demo_scan_data "
                "WHERE scenario = 'history' AND bed_name = ?",
                (bed["bed_key"],),
            )
        }
        for bed in beds
    }
    # Day-major, like real runs: ids follow time, so the history tab's
    # ORDER BY id DESC LIMIT drops the oldest days, not whole seats.
    for days_ago in range(HISTORY_DAYS, 0, -1):
        ts = base - timedelta(days=days_ago)
        day = ts.date().isoformat()
        for bed in beds:
            bed_key = bed["bed_key"]
            if day in have[bed_key]:
                continue
            rec = synth_record("normal", cfg, random.Random(f"{bed_key}:{day}"))
            conn.execute('''
                INSERT INTO demo_scan_data
                (task_id, location_id, bed_name, timestamp, retry_count, status, bpm, rpm,
                 data_json, is_valid, details, scenario)
                VALUES (?, ?, ?, ?, 0, ?, ?, ?, ?, 1, '量測正常', 'history')
            ''', (
                history_task_id(ts), bed.get("location_id") or "", bed_key, ts.isoformat(),
                rec["status"], rec["bpm"], rec["rpm"], json.dumps(rec),
            ))
    conn.commit()
    conn.close()


# ── Reads (for the run summary) ─────────────────────────────────────────────

def scan_rows(task_id: str) -> list[dict]:
    conn = _connect()
    rows = conn.execute(
        "SELECT * FROM demo_scan_data WHERE task_id = ? ORDER BY id", (task_id,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]
