"""IT-21 FEAT-025: heart/respiration threshold alert.

Thresholds are exclusive — a reading sitting exactly on 50/120 or 10/30 is
normal; only a value strictly outside the band alerts. The real patrol path
evaluates it only when ``vitals_alert_enabled`` is on (default off, so 新營 /
板榮 behave exactly as before).
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from common_types import TaskStep
from services import task_runtime
from services.notifications.evaluator import ScanOutcome, VitalsOutOfBandEvaluator
from services.notifications.events import Severity, Source
from settings.defaults import DEFAULT_SETTINGS


def _cfg(**overrides):
    return {**DEFAULT_SETTINGS, **overrides}


def _outcome(bpm, rpm, valid=True, bed="101-1"):
    rec = {"status": 4, "bpm": bpm, "rpm": rpm, "bed_name": bed, "location_id": "loc"}
    return ScanOutcome(
        task_id="t-1", location_id="loc", bed_name=bed,
        valid_record=rec if valid else None, retry_count=0,
        last_record_raw=rec, last_failure_reason=None if valid else "x",
    )


@pytest.mark.parametrize("bpm, rpm", [
    (50, 10), (120, 30), (50, 30), (120, 10), (72, 18),
])
def test_values_on_or_inside_the_band_do_not_alert(bpm, rpm):
    assert VitalsOutOfBandEvaluator().evaluate(_outcome(bpm, rpm), _cfg()) is None


@pytest.mark.parametrize("bpm, rpm", [
    (49, 18), (121, 18), (72, 9), (72, 31), (130, 35),
])
def test_values_strictly_outside_the_band_alert(bpm, rpm):
    event = VitalsOutOfBandEvaluator().evaluate(_outcome(bpm, rpm), _cfg())
    assert event is not None
    assert event.source == Source.VITALS_OUT_OF_BAND
    assert event.severity == Severity.WARN
    assert event.bed_key == "101-1"
    assert event.task_id == "t-1"
    assert event.title == "⚠️ 101-1 心跳呼吸異常"
    assert f"心跳：{bpm}" in event.body
    assert f"呼吸：{rpm}" in event.body
    assert "50–120" in event.body and "10–30" in event.body
    assert event.demo is False


def test_custom_thresholds_are_honoured():
    cfg = _cfg(vitals_hr_low=60, vitals_hr_high=100, vitals_rr_low=12, vitals_rr_high=20)
    assert VitalsOutOfBandEvaluator().evaluate(_outcome(101, 18), cfg) is not None
    assert VitalsOutOfBandEvaluator().evaluate(_outcome(100, 20), cfg) is None


def test_invalid_scan_is_left_to_the_failure_evaluator():
    assert VitalsOutOfBandEvaluator().evaluate(_outcome(0, 0, valid=False), _cfg()) is None


# ── Real patrol path (_do_bio_scan) gating ───────────────────────────────────

def _run_bio_scan(monkeypatch, *, enabled, bpm, rpm):
    captured = []

    async def _capture(event):
        captured.append(event)

    client = MagicMock()
    client.get_valid_scan_data = AsyncMock(return_value=_outcome(bpm, rpm))
    monkeypatch.setattr(task_runtime, "get_bio_sensor_client", lambda: client)
    monkeypatch.setattr(task_runtime, "get_runtime_settings",
                        lambda: _cfg(vitals_alert_enabled=enabled))
    monkeypatch.setattr(task_runtime.dispatcher, "dispatch", _capture)

    engine = task_runtime.TaskEngine(MagicMock(), "kachaka")
    engine.shelf_drop_event = asyncio.Event()
    engine.target_bed = "loc"
    engine.current_task_id = "t-1"
    step = TaskStep(step_id="action_0", action="bio_scan", params={"bed_key": "101-1"})
    result = asyncio.run(engine._do_bio_scan(step))
    return result, captured


def test_switch_off_real_patrol_emits_nothing_for_out_of_band_values(monkeypatch):
    result, captured = _run_bio_scan(monkeypatch, enabled=False, bpm=150, rpm=40)
    assert result.success is True
    assert captured == []


def test_switch_on_real_patrol_emits_a_vitals_alert(monkeypatch):
    result, captured = _run_bio_scan(monkeypatch, enabled=True, bpm=150, rpm=40)
    assert result.success is True
    assert [e.source for e in captured] == [Source.VITALS_OUT_OF_BAND]
    assert captured[0].demo is False


def test_switch_on_in_band_values_stay_silent(monkeypatch):
    _, captured = _run_bio_scan(monkeypatch, enabled=True, bpm=72, rpm=18)
    assert captured == []
