"""F-24 (BIZ-019): saving a schedule whose estimated run overlaps the site's
daily robot reboot window warns — in the response and through the schedule
notice path — but still saves. 板榮 lost two 02:30 runs to the 03:0x reboot.
"""
import asyncio
import json

import pytest

import routers.schedule as schedule_router
from services.notifications import dispatcher
from services.scheduler import scheduler_service

# 120 + 19 × 10 + 60 = 370 s per bed.
CFG = {"bio_scan_initial_wait": 120, "bio_scan_retry_count": 19, "bio_scan_wait_time": 10}


@pytest.fixture
def env(monkeypatch, tmp_path):
    captured = []
    state = {"cfg": dict(CFG, robot_reboot_window="02:50-03:20"), "beds": 10}

    async def _fake_dispatch(event):
        captured.append(event)

    async def _noop_reload():
        pass

    monkeypatch.setattr(dispatcher, "dispatch", _fake_dispatch)
    monkeypatch.setattr(scheduler_service, "reload_from_json", _noop_reload)
    monkeypatch.setattr(schedule_router, "get_runtime_settings", lambda: state["cfg"])

    sched = tmp_path / "schedule.json"
    sched.write_text(json.dumps({"schedules": []}), encoding="utf-8")
    monkeypatch.setattr(schedule_router, "SCHEDULE_FILE", str(sched))

    patrol = tmp_path / "patrol.json"
    monkeypatch.setattr(schedule_router, "PATROL_FILE", str(patrol))

    def _write_beds():
        beds = [{"bed_key": f"B{i}", "enabled": True} for i in range(state["beds"])]
        beds.append({"bed_key": "off", "enabled": False})
        patrol.write_text(json.dumps({"beds_order": beds}), encoding="utf-8")

    state["write_beds"] = _write_beds
    _write_beds()
    state["events"] = captured
    return state


def _save(*entries):
    return asyncio.run(schedule_router.save_schedule({"schedules": list(entries)}))


def _entry(sid, time, enabled=True):
    return {"id": sid, "enabled": enabled, "time": time, "type": "daily"}


def test_run_crossing_the_window_warns_and_still_saves(env):
    # 10 beds × 370 s = 62 min → 02:30 run ends 03:32.
    res = _save(_entry("early", "02:30"))

    assert res["status"] == "ok"
    assert res["data"]["schedules"][0]["id"] == "early"
    assert len(res["warnings"]) == 1
    w = res["warnings"][0]
    assert "early" in w and "03:32" in w and "02:50-03:20" in w
    assert [e.title for e in env["events"]] == ["⚠️ 排程可能撞到機器人每日重開時段"]
    assert w in env["events"][0].body


def test_run_clear_of_the_window_is_quiet(env):
    res = _save(_entry("night", "22:00"))

    assert res["warnings"] == []
    assert env["events"] == []


def test_long_run_reaching_into_the_window_warns(env):
    # 50 beds × 370 s ≈ 5 h 9 min → a 22:00 start ends 03:09, inside 02:50-03:20.
    env["beds"] = 50
    env["write_beds"]()

    res = _save(_entry("night", "22:00"))

    assert len(res["warnings"]) == 1
    assert "03:09" in res["warnings"][0]


def test_window_crossing_midnight(env):
    env["cfg"]["robot_reboot_window"] = "23:50-00:10"

    assert len(_save(_entry("late", "00:05"))["warnings"]) == 1
    assert len(_save(_entry("before", "23:00"))["warnings"]) == 1  # ends 00:02
    assert _save(_entry("after", "00:15"))["warnings"] == []


def test_start_inside_the_window_with_no_beds_still_warns(env):
    env["beds"] = 0
    env["write_beds"]()

    assert len(_save(_entry("x", "03:00"))["warnings"]) == 1


def test_disabled_entries_are_not_checked(env):
    assert _save(_entry("early", "02:30", enabled=False))["warnings"] == []


def test_empty_or_malformed_window_is_off(env):
    env["cfg"]["robot_reboot_window"] = ""
    assert _save(_entry("early", "02:30"))["warnings"] == []

    env["cfg"]["robot_reboot_window"] = "nonsense"
    assert _save(_entry("early", "02:30"))["warnings"] == []

    env["cfg"].pop("robot_reboot_window")
    assert _save(_entry("early", "02:30"))["warnings"] == []


def test_unchanged_entry_is_not_warned_again(env):
    """The page POSTs the whole list; an unrelated toggle must not re-send the
    same warning to the ward (code-review #56)."""
    first = _save(_entry("early", "02:30"))
    assert len(first["warnings"]) == 1
    env["events"].clear()

    again = _save(_entry("early", "02:30"), _entry("late", "23:30", enabled=False))

    assert again["warnings"] == []
    assert env["events"] == []

    retimed = _save(_entry("early", "02:40"))
    assert len(retimed["warnings"]) == 1


def test_estimate_failure_does_not_fail_the_save(env, monkeypatch):
    monkeypatch.setattr(schedule_router, "_reboot_window_warnings",
                        lambda body, before=None: (_ for _ in ()).throw(RuntimeError("bad row")))
    res = _save(_entry("early", "02:30"))
    assert res["status"] == "ok"
    assert res["warnings"] == []
