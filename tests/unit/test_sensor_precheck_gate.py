"""IT-23: start_patrol refuses a patrol when the Wisleep pad is silent.

The pad publishes every ~2.5 s even with nobody on it, so "a message arrived
recently (or arrives while we wait)" is the liveness signal — the broker
connection alone is not (板榮 10/12–10/15: client connected all night, pad dead).
Demo runs never read the pad, so they are not checked; resume is a manual
recovery after a shelf drop and stays ungated.
"""
import asyncio
import types

import pytest
from fastapi import HTTPException

import dependencies
import routers.patrol as patrol
import services.bio_sensor_mqtt as bio_sensor_mqtt
import services.scheduler as scheduler_mod
from common_types import Task, TaskStatus, TaskStep
from services.bio_sensor_mqtt import SENSOR_OFFLINE_PREFIX, SENSOR_SILENT_PREFIX
from services.task_runtime import tasks_db
from tests.unit.test_bio_sensor_scan_manual_default import _make_client

SETTINGS = {
    "shelf_id": "S_04", "demo_preset": "", "patrol_min_battery_pct": 30,
    "mqtt_enabled": True, "sensor_precheck_wait_seconds": 30,
}
PATROL_CFG = {"beds_order": [{"bed_key": "B_101-1", "enabled": True}]}
BEDS_CFG = {"beds": {"B_101-1": {"location_id": "L_101-1"}}}


class _FakeFleet:
    def __init__(self, pct=88.0):
        self.pct = pct

    async def get_battery_info(self, robot_id):
        return {"ok": True, "percentage": self.pct}

    def get_connection_state_dict(self, robot_id, *, debounce_seconds):
        return {"state": "connected"}


class _FakeSensor:
    """wait_for_message stand-in: `result` is returned (or raised) after an
    optional `delay`; `on_wait` runs while the gate is waiting."""

    def __init__(self, result=True, connected=True, delay=0.0, on_wait=None):
        self.result = result
        self.connected = connected
        self.delay = delay
        self.on_wait = on_wait
        self.calls = []

    async def wait_for_message(self, fresh_within_s, timeout_s):
        self.calls.append((fresh_within_s, timeout_s))
        if self.on_wait:
            self.on_wait()
        await asyncio.sleep(self.delay)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


@pytest.fixture
def env(monkeypatch):
    submitted = []
    tasks_db.clear()
    state = {"settings": dict(SETTINGS), "sensor": _FakeSensor()}

    monkeypatch.setattr(patrol, "get_runtime_settings", lambda: dict(state["settings"]))
    monkeypatch.setattr(patrol, "get_bio_sensor_client", lambda: state["sensor"])
    monkeypatch.setattr(
        patrol, "load_json",
        lambda path, default: BEDS_CFG if "beds" in str(path) else PATROL_CFG,
    )
    fleet = _FakeFleet()
    monkeypatch.setattr(dependencies, "get_fleet", lambda: fleet)

    async def _submit(task):
        submitted.append(task)

    monkeypatch.setattr(patrol, "submit_task", _submit)
    # A fresh, unstarted scheduler so retry-job cleanup never touches the global one.
    monkeypatch.setattr(scheduler_mod, "scheduler_service", scheduler_mod.TaskSchedulerService())
    state["submitted"] = submitted
    state["fleet"] = fleet
    yield state
    tasks_db.clear()


def _start(mode="patrol"):
    return asyncio.run(patrol.start_patrol(patrol.PatrolStartRequest(mode=mode)))


def _real_sensor(tmp_path, monkeypatch, *, arrive_on_sleep=None):
    """A real BioSensorMQTTClient with asyncio.sleep faked (instant). When
    `arrive_on_sleep` is set, that payload lands on the first poll."""
    client = _make_client(tmp_path, monkeypatch)
    real_sleep = asyncio.sleep
    polls = {"n": 0}

    async def fake_sleep(_delay):
        polls["n"] += 1
        if arrive_on_sleep is not None and polls["n"] == 1:
            client._ingest(arrive_on_sleep)
        await real_sleep(0)

    monkeypatch.setattr(bio_sensor_mqtt, "asyncio", types.SimpleNamespace(sleep=fake_sleep))
    return client, polls


# ── liveness ────────────────────────────────────────────────────────────────

def test_fresh_message_passes(env, tmp_path, monkeypatch):
    client, polls = _real_sensor(tmp_path, monkeypatch)
    client._ingest({"records": [{"status": 0}]})
    env["sensor"] = client

    assert _start()["status"] == "ok"
    assert polls["n"] == 0  # no waiting when a message is already fresh
    assert len(env["submitted"]) == 1


def test_message_arriving_while_waiting_passes(env, tmp_path, monkeypatch):
    client, polls = _real_sensor(tmp_path, monkeypatch,
                                 arrive_on_sleep={"records": [{"status": 1}]})
    env["sensor"] = client

    assert _start()["status"] == "ok"
    assert polls["n"] == 1
    assert len(env["submitted"]) == 1


def test_silent_pad_is_refused(env, tmp_path, monkeypatch):
    client, polls = _real_sensor(tmp_path, monkeypatch)
    env["sensor"] = client

    with pytest.raises(HTTPException) as exc:
        _start()

    assert exc.value.status_code == 503
    assert exc.value.detail.startswith(SENSOR_SILENT_PREFIX)
    assert "30 秒內沒有收到感測墊訊息" in exc.value.detail
    assert "行動電源" in exc.value.detail
    assert polls["n"] == 60  # 30 s of 0.5 s polls
    assert env["submitted"] == []
    assert tasks_db == {}


def test_gate_waits_the_configured_seconds_with_the_freshness_window(env):
    env["settings"]["sensor_precheck_wait_seconds"] = 12
    _start()
    assert env["sensor"].calls == [(bio_sensor_mqtt.SENSOR_FRESH_WITHIN_S, 12)]


def test_disconnected_client_still_waits_and_passes_on_a_message(env):
    # A broker blip at 22:00 (sensor.log rc=16, reconnect within ~2 s) must not
    # cost the night a 10-minute retry: a pad message during the wait is proof.
    env["sensor"] = _FakeSensor(result=True, connected=False)

    res = _start()

    assert res["status"] == "ok"
    assert len(env["sensor"].calls) == 1


def test_disconnected_client_with_no_message_names_the_link(env):
    env["sensor"] = _FakeSensor(result=False, connected=False)

    with pytest.raises(HTTPException) as exc:
        _start()

    assert exc.value.status_code == 503
    assert exc.value.detail.startswith(SENSOR_OFFLINE_PREFIX)
    assert len(env["sensor"].calls) == 1
    assert env["submitted"] == []


def test_missing_client_is_refused(env):
    env["sensor"] = None

    with pytest.raises(HTTPException) as exc:
        _start()

    assert exc.value.status_code == 503
    assert exc.value.detail.startswith(SENSOR_OFFLINE_PREFIX)


def test_wait_zero_skips_the_gate(env):
    env["settings"]["sensor_precheck_wait_seconds"] = 0
    env["sensor"] = _FakeSensor(result=False, connected=False)

    assert _start()["status"] == "ok"
    assert env["sensor"].calls == []


def test_mqtt_disabled_skips_the_gate(env):
    env["settings"]["mqtt_enabled"] = False
    env["sensor"] = _FakeSensor(result=False, connected=False)

    assert _start()["status"] == "ok"
    assert env["sensor"].calls == []


def test_demo_is_not_checked(env):
    env["sensor"] = _FakeSensor(result=False, connected=False)

    assert _start(mode="demo")["status"] == "ok"
    assert env["sensor"].calls == []


def test_wait_error_fails_open(env):
    env["sensor"] = _FakeSensor(result=RuntimeError("boom"))

    assert _start()["status"] == "ok"
    assert len(env["submitted"]) == 1


def test_battery_gate_runs_first(env):
    env["fleet"].pct = 10.0
    env["sensor"] = _FakeSensor(result=False)

    with pytest.raises(HTTPException) as exc:
        _start()

    assert exc.value.status_code == 400
    assert env["sensor"].calls == []


# ── concurrency ─────────────────────────────────────────────────────────────

def test_concurrent_starts_create_one_task(env, monkeypatch):
    """The gate can wait 30 s; two starts in that window must not both pass the
    dedup that ran before it."""
    monkeypatch.setattr(patrol, "_start_lock", asyncio.Lock())
    env["sensor"] = _FakeSensor(result=True, delay=0.01)

    async def _both():
        req = patrol.PatrolStartRequest(mode="patrol")
        return await asyncio.gather(patrol.start_patrol(req), patrol.start_patrol(req))

    results = asyncio.run(_both())

    assert sorted(r["status"] for r in results) == ["already_running", "ok"]
    assert len(tasks_db) == 1
    assert len(env["submitted"]) == 1
    # The lock queues the second start behind the first, so it is deduped
    # before its own gate — without the lock both would wait out the sensor.
    assert len(env["sensor"].calls) == 1


def test_run_appearing_during_the_gate_is_deduped(env):
    """resume_patrol does not take the start lock — a run it queues while the
    gate waits must still win the dedup."""
    def _resume_lands():
        tasks_db["resumed"] = Task(
            task_id="resumed", robot_id="kachaka",
            steps=[TaskStep(step_id="s", action="bio_scan", params={})],
            status=TaskStatus.QUEUED, metadata={"mode": "patrol"},
        )

    env["sensor"] = _FakeSensor(result=True, on_wait=_resume_lands)

    res = _start()

    assert res["status"] == "already_running"
    assert res["task_id"] == "resumed"
    assert env["submitted"] == []


# ── retry-job cleanup ───────────────────────────────────────────────────────

async def _noop(*_args):
    pass


def _add_jobs(svc):
    from apscheduler.triggers.cron import CronTrigger
    from apscheduler.triggers.date import DateTrigger
    from datetime import datetime, timedelta

    svc.scheduler.add_job(_noop, CronTrigger(hour=23, minute=0), id="patrol_night")
    svc.scheduler.add_job(_noop, DateTrigger(run_date=datetime.now() + timedelta(minutes=10)),
                          id="patrol_night_retry", args=["night", 1])


def test_successful_patrol_start_removes_pending_retry_jobs(env):
    svc = scheduler_mod.scheduler_service
    _add_jobs(svc)

    assert _start()["status"] == "ok"

    assert [j.id for j in svc.scheduler.get_jobs()] == ["patrol_night"]


def test_demo_start_keeps_pending_retry_jobs(env):
    svc = scheduler_mod.scheduler_service
    _add_jobs(svc)

    assert _start(mode="demo")["status"] == "ok"

    assert sorted(j.id for j in svc.scheduler.get_jobs()) == ["patrol_night", "patrol_night_retry"]


def test_refused_start_keeps_pending_retry_jobs(env):
    svc = scheduler_mod.scheduler_service
    _add_jobs(svc)
    env["sensor"] = _FakeSensor(result=False)

    with pytest.raises(HTTPException):
        _start()

    assert sorted(j.id for j in svc.scheduler.get_jobs()) == ["patrol_night", "patrol_night_retry"]


# ── resume stays ungated ────────────────────────────────────────────────────

def test_resume_does_not_call_the_gate(env, monkeypatch):
    """Resume recovers a SHELF_DROPPED run by hand; refusing it would strand the
    task. The in-patrol silent-pad abort covers a dead pad after resume."""
    env["sensor"] = _FakeSensor(result=False, connected=False)

    class _Result:
        success = True
        error_code = 0

    class _Client:
        def reset_shelf_pose(self, shelf_id):
            return _Result()

    class _Fleet:
        def get_raw_client(self, robot_id):
            return _Client()

    monkeypatch.setattr(dependencies, "get_fleet", lambda: _Fleet())
    tasks_db["dropped"] = Task(
        task_id="dropped", robot_id="kachaka",
        steps=[TaskStep(step_id="s", action="bio_scan", params={})],
        status=TaskStatus.SHELF_DROPPED,
        metadata={"shelf_drop": True, "shelf_id": "S_04",
                  "remaining_beds": [{"bed_key": "B_101-1", "location_id": "L_101-1"}]},
    )

    res = asyncio.run(patrol.resume_patrol(patrol.ResumePatrolRequest(task_id="dropped")))

    assert res["status"] == "ok"
    assert env["sensor"].calls == []
    assert len(env["submitted"]) == 1
