"""IT-24 — a refused patrol start is said out loud on the robot.

新民 10/19: three afternoon runs were refused (pad silent since 10/8) and the
person at the Zigbee button only saw a robot that did nothing — the web page
and Telegram are not where they look. Every start path (web, button, the
night schedule) goes through start_patrol, so each gate's refusal plays its
clip there; the HTTP detail stays word-for-word because the scheduler parses
its prefix. The robot-offline gate stays silent (nothing to play it on), and
an announcement that blows up must never turn a refusal into a start.
"""
import asyncio

import pytest
from fastapi import HTTPException

import dependencies
import routers.patrol as patrol
import services.scheduler as scheduler_mod
from services import action_registry, announcer
from services.bio_sensor_mqtt import SENSOR_OFFLINE_PREFIX, SENSOR_SILENT_PREFIX
from services.task_runtime import tasks_db

SETTINGS = {
    "shelf_id": "S_04", "demo_preset": "", "patrol_min_battery_pct": 30,
    "mqtt_enabled": True, "sensor_precheck_wait_seconds": 30,
}
PATROL_CFG = {"beds_order": [{"bed_key": "B_101-1", "enabled": True}]}
BEDS_CFG = {"beds": {"B_101-1": {"location_id": "L_101-1"}}}


class _FakeFleet:
    def __init__(self):
        self.pct = 88.0
        self.conn_state = {"state": "connected"}

    async def get_battery_info(self, robot_id):
        return {"ok": True, "percentage": self.pct}

    def get_connection_state_dict(self, robot_id, *, debounce_seconds):
        return self.conn_state


class _FakeSensor:
    def __init__(self, alive=True, connected=True):
        self.alive = alive
        self.connected = connected

    async def wait_for_message(self, fresh_within_s, timeout_s):
        return self.alive


@pytest.fixture
def env(monkeypatch):
    submitted = []
    tasks_db.clear()
    state = {"sensor": _FakeSensor(), "fleet": _FakeFleet(), "submitted": submitted}

    monkeypatch.setattr(patrol, "get_runtime_settings", lambda: dict(SETTINGS))
    monkeypatch.setattr(patrol, "get_bio_sensor_client", lambda: state["sensor"])
    monkeypatch.setattr(
        patrol, "load_json",
        lambda path, default: BEDS_CFG if "beds" in str(path) else PATROL_CFG,
    )
    monkeypatch.setattr(dependencies, "get_fleet", lambda: state["fleet"])

    async def _submit(task):
        submitted.append(task)

    monkeypatch.setattr(patrol, "submit_task", _submit)
    monkeypatch.setattr(scheduler_mod, "scheduler_service", scheduler_mod.TaskSchedulerService())
    yield state
    tasks_db.clear()


def _refused(mode="patrol"):
    with pytest.raises(HTTPException) as exc:
        asyncio.run(patrol.start_patrol(patrol.PatrolStartRequest(mode=mode)))
    return exc.value


def test_low_battery_announces_and_keeps_the_detail(env, announced):
    env["fleet"].pct = 12.0

    exc = _refused()

    assert exc.status_code == 400
    assert exc.detail == "Battery too low to start patrol: 12% < 30%"
    assert announced == [(announcer.SOUND_BATTERY_LOW, 0.0, False)]
    assert env["submitted"] == []


def test_low_battery_demo_announces_too(env, announced):
    env["fleet"].pct = 12.0

    _refused(mode="demo")

    assert announced == [(announcer.SOUND_BATTERY_LOW, 0.0, False)]


def test_silent_pad_announces_silent(env, announced):
    env["sensor"] = _FakeSensor(alive=False, connected=True)

    exc = _refused()

    assert exc.status_code == 503
    assert exc.detail.startswith(SENSOR_SILENT_PREFIX)
    assert announced == [(announcer.SOUND_SENSOR_SILENT, 0.0, False)]


def test_broker_down_announces_offline(env, announced):
    env["sensor"] = _FakeSensor(alive=False, connected=False)

    exc = _refused()

    assert exc.status_code == 503
    assert exc.detail == f"{SENSOR_OFFLINE_PREFIX}：請檢查網路或憑證設定"
    assert announced == [(announcer.SOUND_SENSOR_OFFLINE, 0.0, False)]


def test_missing_client_announces_offline(env, announced):
    env["sensor"] = None

    exc = _refused()

    assert exc.status_code == 503
    assert exc.detail == f"{SENSOR_OFFLINE_PREFIX}：請檢查網路或憑證設定"
    assert announced == [(announcer.SOUND_SENSOR_OFFLINE, 0.0, False)]


def test_offline_robot_refuses_silently(env, announced):
    env["fleet"].conn_state = {"state": "disconnected", "offline_pending": False}

    exc = _refused()

    assert exc.status_code == 503
    assert announced == []


def test_no_enabled_beds_refuses_silently(env, announced, monkeypatch):
    monkeypatch.setattr(
        patrol, "load_json",
        lambda path, default: BEDS_CFG if "beds" in str(path) else {"beds_order": []},
    )

    exc = _refused()

    assert exc.status_code == 400
    assert announced == []


def test_passing_start_announces_nothing(env, announced):
    res = asyncio.run(patrol.start_patrol(patrol.PatrolStartRequest(mode="patrol")))

    assert res["status"] == "ok"
    assert announced == []


@pytest.mark.parametrize("pct,sensor,detail_start", [
    (12.0, _FakeSensor(), "Battery too low to start patrol: 12% < 30%"),
    (88.0, _FakeSensor(alive=False, connected=True), SENSOR_SILENT_PREFIX),
])
def test_announce_that_raises_still_refuses(env, monkeypatch, pct, sensor, detail_start):
    async def _boom(name, *, pre_delay_s=0.0, wait=False):
        raise RuntimeError("jupyter down")

    monkeypatch.setattr(announcer, "announce", _boom)
    env["fleet"].pct = pct
    env["sensor"] = sensor

    exc = _refused()

    assert exc.detail.startswith(detail_start)
    assert env["submitted"] == []


def test_scheduled_run_announces(env, announced, monkeypatch):
    """The 22:00 run is unattended on the web side — the ward hears it."""
    notices = []

    async def _notify(title, body, raw=None):
        notices.append(body)

    monkeypatch.setattr(scheduler_mod, "notify_schedule_issue", _notify)
    env["fleet"].pct = 12.0

    asyncio.run(scheduler_mod.scheduler_service._run_patrol("night"))

    assert announced == [(announcer.SOUND_BATTERY_LOW, 0.0, False)]
    assert "電量 12% 低於門檻 30%" in notices[0]


def test_button_press_announces(env, announced):
    action_registry.reset()
    action_registry.init_default_actions()
    env["sensor"] = _FakeSensor(alive=False, connected=True)
    try:
        result = asyncio.run(action_registry.fire("patrol_start"))
    finally:
        action_registry.reset()

    assert result["ok"] is False
    assert announced == [(announcer.SOUND_SENSOR_SILENT, 0.0, False)]
