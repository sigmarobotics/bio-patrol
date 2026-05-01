"""CORNER-005 — POST /api/patrol/resume-latest with no shelf-dropped task.

The shelf_resume Zigbee button calls this endpoint without a task_id; if
nothing is in tasks_db with metadata.shelf_drop=True, the endpoint must
return 400 (so the button gives the operator feedback) — never 500.
"""
from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest
from fastapi import HTTPException

from common_types import Task, TaskStatus, TaskStep
from routers.patrol import resume_latest_shelf_drop


def test_no_tasks_at_all_returns_400():
    with patch("routers.patrol.tasks_db", {}):
        with pytest.raises(HTTPException) as exc:
            asyncio.run(resume_latest_shelf_drop())
        assert exc.value.status_code == 400
        assert "shelf-dropped task" in exc.value.detail.lower()


def test_tasks_exist_but_none_shelf_dropped_returns_400():
    fake_db = {
        "t-done": Task(task_id="t-done", robot_id="kachaka", steps=[], status=TaskStatus.DONE),
        "t-running": Task(task_id="t-running", robot_id="kachaka", steps=[], status=TaskStatus.IN_PROGRESS),
    }
    with patch("routers.patrol.tasks_db", fake_db):
        with pytest.raises(HTTPException) as exc:
            asyncio.run(resume_latest_shelf_drop())
        assert exc.value.status_code == 400


def test_already_done_shelf_drop_does_not_count():
    """A previously-resumed shelf drop (now DONE) should NOT match — otherwise
    pressing the button after a successful resume would re-resume the same task."""
    done_drop = Task(
        task_id="t-old",
        robot_id="kachaka",
        steps=[TaskStep(step_id="s-1", action="bio_scan", params={})],
        status=TaskStatus.DONE,
        metadata={"shelf_drop": True, "shelf_id": "S_04"},
    )
    with patch("routers.patrol.tasks_db", {"t-old": done_drop}):
        with pytest.raises(HTTPException) as exc:
            asyncio.run(resume_latest_shelf_drop())
        assert exc.value.status_code == 400
