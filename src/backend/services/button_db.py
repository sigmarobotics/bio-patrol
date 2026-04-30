"""SQLite helpers for the button_bindings table.

Single-table schema — one row per action_key. NULL ieee_addr means "action
listed in the UI but no button paired yet". Stored alongside bio-sensor data
in `data/sensor_data.db`.
"""
from __future__ import annotations

import logging
import os
import sqlite3
from typing import Any

logger = logging.getLogger("services.button_db")


def _project_root() -> str:
    return os.path.dirname(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    )


DEFAULT_DB_PATH = os.path.join(_project_root(), "data", "sensor_data.db")
TABLE_NAME = "button_bindings"


def _connect(db_path: str | None = None) -> sqlite3.Connection:
    path = db_path or DEFAULT_DB_PATH
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def _now_iso() -> str:
    # Late import — common_types pulls the runtime settings (timezone). Keeping
    # the import inside the function lets unit tests run without setting up the
    # full settings/config stack.
    from common_types import get_now
    return get_now().isoformat()


def init_schema(db_path: str | None = None) -> None:
    """Create the button_bindings table + IEEE uniqueness index if missing."""
    with _connect(db_path) as conn:
        conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {TABLE_NAME} (
                action_key      TEXT PRIMARY KEY,
                ieee_addr       TEXT,
                friendly_name   TEXT,
                paired_at       TEXT,
                battery         INTEGER,
                last_seen       TEXT,
                last_fired_at   TEXT,
                fire_count      INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        conn.execute(
            f"CREATE UNIQUE INDEX IF NOT EXISTS idx_{TABLE_NAME}_ieee "
            f"ON {TABLE_NAME}(ieee_addr) WHERE ieee_addr IS NOT NULL"
        )
        conn.commit()


def seed_actions(action_keys: list[str], db_path: str | None = None) -> None:
    with _connect(db_path) as conn:
        for key in action_keys:
            conn.execute(
                f"INSERT OR IGNORE INTO {TABLE_NAME} (action_key) VALUES (?)",
                (key,),
            )
        conn.commit()


def list_bindings(db_path: str | None = None) -> list[dict[str, Any]]:
    with _connect(db_path) as conn:
        rows = conn.execute(
            f"SELECT action_key, ieee_addr, friendly_name, paired_at, battery, "
            f"last_seen, last_fired_at, fire_count FROM {TABLE_NAME} "
            f"ORDER BY action_key"
        ).fetchall()
    return [dict(r) for r in rows]


def get_binding_by_ieee(ieee_addr: str,
                        db_path: str | None = None) -> dict[str, Any] | None:
    with _connect(db_path) as conn:
        row = conn.execute(
            f"SELECT action_key, ieee_addr, friendly_name, paired_at, battery, "
            f"last_seen, last_fired_at, fire_count FROM {TABLE_NAME} "
            f"WHERE ieee_addr = ?",
            (ieee_addr,),
        ).fetchone()
    return dict(row) if row else None


def bind_action(action_key: str, ieee_addr: str, friendly_name: str | None,
                db_path: str | None = None) -> None:
    """Bind ieee_addr to action_key, clearing any other action that held it."""
    now = _now_iso()
    with _connect(db_path) as conn:
        conn.execute(
            f"UPDATE {TABLE_NAME} SET ieee_addr = NULL, friendly_name = NULL, "
            f"paired_at = NULL WHERE ieee_addr = ? AND action_key != ?",
            (ieee_addr, action_key),
        )
        conn.execute(
            f"INSERT INTO {TABLE_NAME} (action_key, ieee_addr, friendly_name, paired_at) "
            f"VALUES (?, ?, ?, ?) ON CONFLICT(action_key) DO UPDATE SET "
            f"ieee_addr = excluded.ieee_addr, friendly_name = excluded.friendly_name, "
            f"paired_at = excluded.paired_at",
            (action_key, ieee_addr, friendly_name, now),
        )
        conn.commit()


def unbind_action(action_key: str, db_path: str | None = None) -> str | None:
    """Clear the binding. Returns the previously-bound IEEE, if any."""
    with _connect(db_path) as conn:
        row = conn.execute(
            f"SELECT ieee_addr FROM {TABLE_NAME} WHERE action_key = ?",
            (action_key,),
        ).fetchone()
        prev = row["ieee_addr"] if row else None
        conn.execute(
            f"UPDATE {TABLE_NAME} SET ieee_addr = NULL, friendly_name = NULL, "
            f"paired_at = NULL, battery = NULL, last_seen = NULL "
            f"WHERE action_key = ?",
            (action_key,),
        )
        conn.commit()
    return prev


def update_status(ieee_addr: str, battery: int | None, last_seen: str,
                  db_path: str | None = None) -> None:
    with _connect(db_path) as conn:
        conn.execute(
            f"UPDATE {TABLE_NAME} SET battery = COALESCE(?, battery), last_seen = ? "
            f"WHERE ieee_addr = ?",
            (battery, last_seen, ieee_addr),
        )
        conn.commit()


def record_fire(action_key: str, db_path: str | None = None) -> None:
    now = _now_iso()
    with _connect(db_path) as conn:
        conn.execute(
            f"UPDATE {TABLE_NAME} SET last_fired_at = ?, fire_count = fire_count + 1 "
            f"WHERE action_key = ?",
            (now, action_key),
        )
        conn.commit()
