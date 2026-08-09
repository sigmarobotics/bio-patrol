"""SQLite connection helper hardened against hard power cuts.

The edge device gets unplugged without warning. The default rollback-journal
mode corrupts easily under that; WAL survives it. `journal_mode` persists in
the DB file, but `synchronous` is per-connection — so every sqlite3.connect()
in the backend goes through this helper instead of calling sqlite3 directly.
"""
from __future__ import annotations

import logging
import sqlite3
from typing import Any

logger = logging.getLogger(__name__)


def connect_db(db_path: str, **kwargs: Any) -> sqlite3.Connection:
    """Open a SQLite connection with WAL + synchronous=NORMAL applied."""
    conn = sqlite3.connect(db_path, **kwargs)
    # SQLite returns the resulting mode; a busy DB or a filesystem without
    # WAL support silently leaves it on rollback-journal — surface that.
    mode = conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]
    if mode != "wal":
        logger.warning(
            "journal_mode=WAL did not take on %s (got %r) — power-loss hardening inactive",
            db_path,
            mode,
        )
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn
