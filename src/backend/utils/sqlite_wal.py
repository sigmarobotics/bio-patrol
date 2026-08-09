"""SQLite connection helper hardened against hard power cuts.

The edge device gets unplugged without warning. The default rollback-journal
mode corrupts easily under that; WAL survives it. `journal_mode` persists in
the DB file, but `synchronous` is per-connection — so every sqlite3.connect()
in the backend goes through this helper instead of calling sqlite3 directly.
"""
from __future__ import annotations

import sqlite3
from typing import Any


def connect_db(db_path: str, **kwargs: Any) -> sqlite3.Connection:
    """Open a SQLite connection with WAL + synchronous=NORMAL applied."""
    conn = sqlite3.connect(db_path, **kwargs)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn
