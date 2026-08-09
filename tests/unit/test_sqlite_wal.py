"""Pins the power-loss hardening pragmas — see utils/sqlite_wal.py."""
from __future__ import annotations

import os
import tempfile

from utils.sqlite_wal import connect_db


def test_connect_db_applies_wal_and_normal_sync():
    with tempfile.TemporaryDirectory() as d:
        conn = connect_db(os.path.join(d, "t.db"))
        try:
            assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
            assert conn.execute("PRAGMA synchronous").fetchone()[0] == 1  # NORMAL
        finally:
            conn.close()
