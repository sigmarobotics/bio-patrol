"""F-20 (BIZ-019): resume and recover-shelf are idempotent and refuse during a
live run.

A double tap on 歸位並繼續巡房, or two Zigbee shelf_resume presses inside one
reset_shelf_pose RPC, used to queue two resumed patrols. And a nurse pressing
續巡/歸位 on a stale drop overlay while the next patrol runs sent
reset_shelf_pose to a robot carrying the shelf mid-route. The user overrode
CORNER-081 for the live-run case.
"""
from __future__ import annotations

import asyncio
import time

import pytest
from fastapi import HTTPException

import dependencies
import routers.patrol as patrol
from common_types import Task, TaskStatus, TaskStep
from services.task_runtime import tasks_db


class _Result:
    success = True
    error_code = 0


class _Client:
    def __init__(self, delay=0.0, success=True):
        self.calls = []
        self.delay = delay
        self.success = success

    def reset_shelf_pose(self, shelf_id):
        self.calls.append(shelf_id)
        if self.delay:
            time.sleep(self.delay)
        r = _Result()
        r.success = self.success
        return r


@pytest.fixture
def env(monkeypatch):
    tasks_db.clear()
    patrol._resuming.clear()
    client = _Client()
    submitted = []

    class _Fleet:
        def get_raw_client(self, robot_id):
            return client

    async def _submit(task):
        submitted.append(task)

    monkeypatch.setattr(dependencies, "get_fleet", lambda: _Fleet())
    monkeypatch.setattr(patrol, "submit_task", _submit)
    monkeypatch.setattr(patrol, "get_runtime_settings", lambda: {})
    monkeypatch.setattr(patrol, "clear_shelf_dropped_tasks", lambda reason="": 0)
    yield {"client": client, "submitted": submitted}
    tasks_db.clear()
    patrol._resuming.clear()


def _drop(task_id="dropped"):
    tasks_db[task_id] = Task(
        task_id=task_id, robot_id="kachaka",
        steps=[TaskStep(step_id="s", action="bio_scan", params={})],
        status=TaskStatus.SHELF_DROPPED,
        metadata={"shelf_drop": True, "shelf_id": "S_04", "dropped_at": task_id,
                  "remaining_beds": [{"bed_key": "B_101-1", "location_id": "L_101-1"}]},
    )


def _live(status=TaskStatus.IN_PROGRESS):
    tasks_db["live"] = Task(
        task_id="live", robot_id="kachaka",
        steps=[TaskStep(step_id="s", action="bio_scan", params={})],
        status=status, metadata={"mode": "patrol"},
    )


def _resume(task_id="dropped"):
    return patrol.resume_patrol(patrol.ResumePatrolRequest(task_id=task_id))


# ── (a) idempotency ──────────────────────────────────────────────────────────

def test_two_sequential_resumes_submit_one_task(env):
    _drop()

    assert asyncio.run(_resume())["status"] == "ok"
    with pytest.raises(HTTPException) as exc:
        asyncio.run(_resume())

    assert exc.value.status_code == 409
    assert exc.value.detail == patrol.RESUME_DUPLICATE_DETAIL
    assert len(env["submitted"]) == 1
    assert env["client"].calls == ["S_04"]


def test_two_gathered_resumes_submit_one_task(env):
    _drop()
    env["client"].delay = 0.05

    async def _both():
        return await asyncio.gather(_resume(), _resume(), return_exceptions=True)

    results = asyncio.run(_both())

    oks = [r for r in results if isinstance(r, dict)]
    refused = [r for r in results if isinstance(r, HTTPException)]
    assert len(oks) == 1 and len(refused) == 1
    assert refused[0].status_code == 409
    assert len(env["submitted"]) == 1
    assert env["client"].calls == ["S_04"]


def test_two_gathered_zigbee_presses_submit_one_task(env):
    _drop()
    env["client"].delay = 0.05

    async def _both():
        return await asyncio.gather(
            patrol.resume_latest_shelf_drop(), patrol.resume_latest_shelf_drop(),
            return_exceptions=True,
        )

    results = asyncio.run(_both())

    assert sum(isinstance(r, dict) for r in results) == 1
    assert len(env["submitted"]) == 1


def test_failed_reset_releases_the_claim(env):
    _drop()
    env["client"].success = False

    with pytest.raises(HTTPException) as exc:
        asyncio.run(_resume())
    assert exc.value.status_code == 502
    assert tasks_db["dropped"].status == TaskStatus.SHELF_DROPPED

    env["client"].success = True
    assert asyncio.run(_resume())["status"] == "ok"
    assert len(env["submitted"]) == 1


# ── (b) refuse during a live run ─────────────────────────────────────────────

@pytest.mark.parametrize("status", [TaskStatus.QUEUED, TaskStatus.IN_PROGRESS])
def test_resume_during_live_run_is_409_and_sends_no_reset(env, status):
    _drop()
    _live(status)

    with pytest.raises(HTTPException) as exc:
        asyncio.run(_resume())

    assert exc.value.status_code == 409
    assert exc.value.detail == "巡房進行中，請等目前巡房結束"
    assert env["client"].calls == []
    assert env["submitted"] == []
    assert tasks_db["dropped"].status == TaskStatus.SHELF_DROPPED


def test_zigbee_resume_during_live_run_is_409(env):
    _drop()
    _live()

    with pytest.raises(HTTPException) as exc:
        asyncio.run(patrol.resume_latest_shelf_drop())

    assert exc.value.status_code == 409
    assert exc.value.detail == "巡房進行中，請等目前巡房結束"
    assert env["client"].calls == []


def test_run_starting_during_the_reset_refuses_the_resume(env, monkeypatch):
    """A start whose gates were awaiting may queue a run during the reset RPC;
    the resume must not queue a second one behind it."""
    _drop()

    class _ClientStartsRun(_Client):
        def reset_shelf_pose(self, shelf_id):
            _live(TaskStatus.QUEUED)
            return super().reset_shelf_pose(shelf_id)

    client = _ClientStartsRun()

    class _Fleet:
        def get_raw_client(self, robot_id):
            return client

    monkeypatch.setattr(dependencies, "get_fleet", lambda: _Fleet())

    with pytest.raises(HTTPException) as exc:
        asyncio.run(_resume())

    assert exc.value.detail == "巡房進行中，請等目前巡房結束"
    assert env["submitted"] == []
    assert tasks_db["dropped"].status == TaskStatus.SHELF_DROPPED


def test_recover_shelf_during_live_run_is_409_and_sends_no_reset(env):
    _drop()
    _live()

    with pytest.raises(HTTPException) as exc:
        asyncio.run(patrol.recover_shelf(patrol.RecoverShelfRequest(shelf_id="S_04")))

    assert exc.value.status_code == 409
    assert exc.value.detail == "巡房進行中，請等目前巡房結束"
    assert env["client"].calls == []


def test_recover_shelf_without_live_run_still_works(env):
    _drop()

    res = asyncio.run(patrol.recover_shelf(patrol.RecoverShelfRequest(shelf_id="S_04")))

    assert res["status"] == "ok"
    assert env["client"].calls == ["S_04"]
