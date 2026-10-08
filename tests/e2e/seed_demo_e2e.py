"""IT-21: seed one finished demo run into data/demo_data.db for
test_demo_panel.spec.js.

tasks_db is in-memory, so a script cannot plant a task in a running server.
Instead this runs a real 3-seat demo task through the production TaskEngine
(fake fleet, 0-second dwells) in THIS process: real synthesis, real
evaluators, real dispatcher → DemoPreviewSink, real run summary. The server
then renders it through /api/demo/live's "last demo result" path.

Also applies the hardware-less settings seed_e2e.py uses (zigbee off,
non-routable robot IP), so the robot reads as disconnected.

Run from repo root:
    PYTHONPATH=src/backend uv run python tests/e2e/seed_demo_e2e.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
from unittest.mock import AsyncMock, MagicMock

from common_types import StepAction, Task, TaskStatus, TaskStep, generate_task_id
from services import demo_data, task_runtime
from services.notifications.dispatcher import dispatcher
from services.notifications.sinks.demo_preview import DemoPreviewSink
from settings.config import SETTINGS_FILE, get_runtime_settings

SEATS = ["S01", "S02", "S03"]   # → normal, abnormal, absent (route order)

# Hardware-less server settings (same as seed_e2e.py).
settings = {}
if os.path.exists(SETTINGS_FILE):
    with open(SETTINGS_FILE) as f:
        settings = json.load(f)
settings["zigbee_enabled"] = False
settings["robot_ip"] = "127.0.0.1:26400"
os.makedirs(os.path.dirname(SETTINGS_FILE), exist_ok=True)
with open(SETTINGS_FILE, "w") as f:
    json.dump(settings, f, indent=2)

# Deterministic: drop every earlier demo row, keep the schema.
if os.path.exists(demo_data.DB_PATH):
    conn = sqlite3.connect(demo_data.DB_PATH)
    conn.execute("DELETE FROM demo_scan_data")
    conn.execute("DELETE FROM demo_notifications")
    conn.commit()
    conn.close()

demo_data.ensure_history(SEATS, get_runtime_settings())

steps = [
    TaskStep(step_id=f"action_{i}", action=StepAction.DEMO_SCAN.value, params={
        "bed_key": bed, "location_id": f"loc_{bed}",
        "scenario": demo_data.scenario_for(i), "seconds": 0,
    })
    for i, bed in enumerate(SEATS)
]
task = Task(task_id=generate_task_id(), robot_id="kachaka", steps=steps,
            status=TaskStatus.QUEUED, metadata={"mode": "demo"})

fleet = MagicMock()
fleet.get_shelves = AsyncMock(return_value={"ok": True, "shelves": []})
fleet.get_locations = AsyncMock(return_value={"ok": True, "locations": []})
fleet.get_battery_info = AsyncMock(return_value={"ok": False})
fleet.get_metrics = AsyncMock(return_value={"poll_count": 0, "poll_rtt_list": [], "poll_success_count": 0})
fleet.reset_metrics = AsyncMock(return_value=None)
fleet.get_slot_or_none = MagicMock(return_value=None)
fleet._robots = {}

dispatcher.register(DemoPreviewSink())


async def _run():
    done = await task_runtime.TaskEngine(fleet, "kachaka").run_task(task)
    await dispatcher.drain(timeout=5.0)
    return done


done = asyncio.run(_run())
print(f"Seeded demo run {done.task_id} ({done.status.value}) into {demo_data.DB_PATH}")
for row in demo_data.scan_rows(done.task_id):
    print(f"  {row['bed_name']}: {row['scenario']} status={row['status']} bpm={row['bpm']} rpm={row['rpm']}")
_, items = demo_data.notification_rows(done.task_id)
for n in items:
    print(f"  notify: {n['title']}")
