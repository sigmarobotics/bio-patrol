"""IT-21b: seed one finished demo run + its 30-day synthetic history into
data/demo_data.db for test_demo_view.spec.js.

Run AFTER seed_e2e.py: the demo seats are seed_e2e.py's beds (beds.json), so
/demo renders the same bed cards as / — only from the demo DB.

tasks_db is in-memory, so a script cannot plant a task in a running server.
Instead this runs a real 3-seat demo task through the production TaskEngine
(fake fleet, 0-second dwells) in THIS process: real synthesis, real
evaluators, real run summary. Route order gives 101-1 normal, 101-2 abnormal,
102-1 absent.

Run from repo root:
    PYTHONPATH=src/backend uv run python tests/e2e/seed_demo_e2e.py
"""
from __future__ import annotations

import asyncio
import os
import sqlite3
from unittest.mock import AsyncMock, MagicMock

from common_types import StepAction, Task, TaskStatus, TaskStep, generate_task_id
from services import demo_data, task_runtime
from settings.config import get_runtime_settings

# Same beds + location_ids as seed_e2e.py.
SEATS = [
    {"bed_key": "101-1", "location_id": "loc_101_1"},   # → normal
    {"bed_key": "101-2", "location_id": "loc_101_2"},   # → abnormal
    {"bed_key": "102-1", "location_id": "loc_102_1"},   # → absent
]

# Deterministic: drop every earlier demo row, keep the schema.
if os.path.exists(demo_data.DB_PATH):
    conn = sqlite3.connect(demo_data.DB_PATH)
    conn.execute("DELETE FROM demo_scan_data")
    conn.commit()
    conn.close()

demo_data.ensure_history(SEATS, get_runtime_settings())

steps = [
    TaskStep(step_id=f"action_{i}", action=StepAction.DEMO_SCAN.value, params={
        **seat, "scenario": demo_data.scenario_for(i), "seconds": 0,
    })
    for i, seat in enumerate(SEATS)
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

done = asyncio.run(task_runtime.TaskEngine(fleet, "kachaka").run_task(task))
print(f"Seeded demo run {done.task_id} ({done.status.value}) into {demo_data.DB_PATH}")
for row in demo_data.scan_rows(done.task_id):
    print(f"  {row['bed_name']}: {row['scenario']} status={row['status']} bpm={row['bpm']} rpm={row['rpm']}")
