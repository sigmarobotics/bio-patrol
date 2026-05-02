"""IT-9 Slice 4: deterministic E2E seed.

Creates `data/config/beds.json` + `patrol.json` and injects representative
rows into `data/sensor_data.db` so the dashboard renders cards for ALL FOUR
states (valid, stale, invalid, unscheduled).

Run from repo root:
    PYTHONPATH=src/backend uv run python tests/e2e/seed_e2e.py
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import uuid
from datetime import datetime, timedelta, timezone

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CONFIG_DIR = os.path.join(REPO_ROOT, "data", "config")
DB_PATH = os.path.join(REPO_ROOT, "data", "sensor_data.db")

os.makedirs(CONFIG_DIR, exist_ok=True)
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

# 4 beds spanning 2 rooms — gives us enough rooms for collapse/expand test
BEDS = {
    "101-1": {"room": "101", "bed": 1, "location_id": "loc_101_1"},  # → valid
    "101-2": {"room": "101", "bed": 2, "location_id": "loc_101_2"},  # → invalid
    "102-1": {"room": "102", "bed": 1, "location_id": "loc_102_1"},  # → stale
    "102-2": {"room": "102", "bed": 2, "location_id": "loc_102_2"},  # → unscheduled (enabled=False)
}

beds_json = {
    "room_count": 14,
    "room_start": 101,
    "bed_numbers": [1, 2],
    "beds": BEDS,
}

# Patrol: enable all beds EXCEPT 102-2
patrol_json = {
    "beds_order": [
        {"bed_key": "101-1", "enabled": True},
        {"bed_key": "101-2", "enabled": True},
        {"bed_key": "102-1", "enabled": True},
        {"bed_key": "102-2", "enabled": False},
    ],
}

with open(os.path.join(CONFIG_DIR, "beds.json"), "w") as f:
    json.dump(beds_json, f, indent=2)
with open(os.path.join(CONFIG_DIR, "patrol.json"), "w") as f:
    json.dump(patrol_json, f, indent=2)

# settings.json — mqtt_enabled=True is required so /latest-by-bed runs the
# sqlite query (returns 'disabled' otherwise). Broker connect failure during
# start() is caught + logged; the client object is still kept on the module
# global, so the GET handler succeeds.
settings_path = os.path.join(CONFIG_DIR, "settings.json")
existing_settings = {}
if os.path.exists(settings_path):
    try:
        with open(settings_path) as f:
            existing_settings = json.load(f)
    except Exception:
        existing_settings = {}
existing_settings["mqtt_enabled"] = True
existing_settings.setdefault("mqtt_broker", "localhost")
existing_settings.setdefault("mqtt_port", 1883)
# Disable zigbee + force a non-routable robot IP so lifespan startup doesn't
# wait on hardware that isn't here.
existing_settings["zigbee_enabled"] = False
existing_settings["robot_ip"] = "127.0.0.1:26400"
existing_settings.setdefault("bed_card_stale_hours", 24)
with open(settings_path, "w") as f:
    json.dump(existing_settings, f, indent=2)
print(f"Wrote beds.json + patrol.json + settings.json to {CONFIG_DIR}")

# Init sensor DB
conn = sqlite3.connect(DB_PATH)
cur = conn.cursor()
cur.execute(
    """
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
    """
)
# Migration: add bed_name to pre-existing seed DBs that predate this column
try:
    cur.execute("ALTER TABLE sensor_scan_data ADD COLUMN bed_name TEXT NULL")
except sqlite3.OperationalError:
    pass  # column already exists

# Wipe E2E rows from prior runs to make seed deterministic
cur.execute("DELETE FROM sensor_scan_data WHERE task_id LIKE 'e2e-%'")

now = datetime.now(timezone.utc)


def insert(loc_id: str, ts: datetime, *, is_valid: bool, status: int, bpm: int, rpm: int, details: str | None = None):
    data = {"location_id": loc_id, "status": status, "bpm": bpm, "rpm": rpm, "details": details}
    cur.execute(
        """
        INSERT INTO sensor_scan_data
        (task_id, location_id, timestamp, retry_count, status, bpm, rpm, data_json, is_valid, details)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            f"e2e-{uuid.uuid4()}",
            loc_id,
            ts.isoformat(),
            0,
            status,
            bpm,
            rpm,
            json.dumps(data),
            is_valid,
            details,
        ),
    )


# 101-1 → VALID (recent + is_valid=True)
insert("loc_101_1", now - timedelta(minutes=5), is_valid=True, status=4, bpm=72, rpm=18)

# 101-2 → INVALID (recent but is_valid=False)
insert("loc_101_2", now - timedelta(minutes=3), is_valid=False, status=2, bpm=0, rpm=0, details="Signal too weak")

# 102-1 → STALE (last valid > 24h ago, threshold is 24h)
insert("loc_102_1", now - timedelta(hours=30), is_valid=True, status=4, bpm=80, rpm=20)

# 102-2 → UNSCHEDULED (enabled=False) — even though there's a valid recent row, classifier ignores it
insert("loc_102_2", now - timedelta(minutes=10), is_valid=True, status=4, bpm=70, rpm=17)

conn.commit()
conn.close()

print(f"Seeded sensor_data.db at {DB_PATH}")
print("State map:")
print("  101-1 → valid     (recent valid)")
print("  101-2 → invalid   (recent is_valid=False)")
print("  102-1 → stale     (>24h ago)")
print("  102-2 → unscheduled (patrol disabled)")

# 3 size-diverse maps for test_map_switch.spec.js
from PIL import Image, ImageDraw

MAPS_DIR = os.path.join(REPO_ROOT, "data", "maps")
os.makedirs(MAPS_DIR, exist_ok=True)

E2E_MAPS = [
    {"id": "e2e-map-small", "name": "E2E small", "w": 100, "h": 80},
    {"id": "e2e-map-medium", "name": "E2E medium", "w": 400, "h": 300},
    {"id": "e2e-map-large", "name": "E2E large", "w": 1200, "h": 900},
]

# Wipe prior e2e- maps to keep seed deterministic
for fname in os.listdir(MAPS_DIR):
    if fname.startswith("e2e-map-"):
        os.remove(os.path.join(MAPS_DIR, fname))

for m in E2E_MAPS:
    img = Image.new("RGB", (m["w"], m["h"]), color=(245, 235, 215))
    draw = ImageDraw.Draw(img)
    # Outer rectangle
    draw.rectangle([(2, 2), (m["w"] - 3, m["h"] - 3)], outline=(60, 60, 60), width=2)
    # Diagonal lines so it's clear which map is which
    draw.line([(0, 0), (m["w"], m["h"])], fill=(60, 60, 60), width=2)
    draw.line([(m["w"], 0), (0, m["h"])], fill=(60, 60, 60), width=2)
    img.save(os.path.join(MAPS_DIR, f"{m['id']}.png"))
    meta = {
        "id": m["id"],
        "name": m["name"],
        "robot_map_id": m["id"],
        "timestamp": now.isoformat(),
        "resolution": 0.05,
        "width": m["w"],
        "height": m["h"],
        "origin": {"x": -m["w"] * 0.05 / 2, "y": -m["h"] * 0.05 / 2},
        "locations": [],
    }
    with open(os.path.join(MAPS_DIR, f"{m['id']}.json"), "w") as f:
        json.dump(meta, f, indent=2)

print(f"Seeded {len(E2E_MAPS)} size-diverse maps under {MAPS_DIR}")
