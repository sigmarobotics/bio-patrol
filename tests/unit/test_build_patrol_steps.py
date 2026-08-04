"""build_patrol_steps is shared between routers/patrol.py and services/scheduler.py.
Pin its contract so a refactor in one consumer can't silently drift the other.
"""
from __future__ import annotations

import pytest

from common_types import StepAction, StepStatus
from routers.patrol import build_patrol_steps


def _beds():
    return [
        {"bed_key": "101-1", "location_id": "loc-101-1"},
        {"bed_key": "101-2", "location_id": "loc-101-2"},
    ]


def test_patrol_mode_emits_move_then_bio_scan_per_bed_then_return_shelf():
    steps = build_patrol_steps(_beds(), shelf_id="S_04", mode="patrol")
    assert [s.action for s in steps] == [
        StepAction.RESET_SHELF_POSE.value,
        StepAction.MOVE_SHELF.value, StepAction.BIO_SCAN.value,
        StepAction.MOVE_SHELF.value, StepAction.BIO_SCAN.value,
        StepAction.RETURN_SHELF.value,
    ]
    # The bio_scan step must carry bed_key — scheduler used to drop it (latent bug).
    bio_scan_steps = [s for s in steps if s.action == StepAction.BIO_SCAN.value]
    assert [s.params["bed_key"] for s in bio_scan_steps] == ["101-1", "101-2"]
    # Each move step's failure should skip the matching action step.
    move_steps = [s for s in steps if s.action == StepAction.MOVE_SHELF.value]
    for move, bio in zip(move_steps, bio_scan_steps):
        assert move.skip_on_failure == [bio.step_id]


def test_demo_mode_uses_wait_5s_instead_of_bio_scan():
    steps = build_patrol_steps(_beds(), shelf_id="S_04", mode="demo")
    actions = [s.action for s in steps]
    assert StepAction.BIO_SCAN.value not in actions
    wait_steps = [s for s in steps if s.action == StepAction.WAIT.value]
    assert len(wait_steps) == 2
    assert all(s.params["seconds"] == 5 for s in wait_steps)


def test_empty_beds_emits_no_steps_and_no_dangling_return_shelf():
    steps = build_patrol_steps([], shelf_id="S_04", mode="patrol")
    assert steps == []


def test_all_invalid_beds_emit_no_steps():
    """start/resume return 400 on an empty step list — a run that reduces to
    nothing must not become a lone reset_shelf_pose step."""
    beds = [{"bed_key": "", "location_id": ""}, {"bed_key": "101-1", "location_id": ""}]
    steps = build_patrol_steps(beds, shelf_id="S_04", mode="patrol")
    assert steps == []


@pytest.mark.parametrize("mode", ["patrol", "demo"])
def test_run_opens_with_reset_shelf_pose(mode):
    """The customer parks the shelf at home before every run, so the run opens
    by resetting the robot's shelf-pose estimate to home."""
    steps = build_patrol_steps(_beds(), shelf_id="S_04", mode=mode)
    first = steps[0]
    assert first.action == StepAction.RESET_SHELF_POSE.value
    assert first.step_id == "reset_shelf"
    assert first.params == {"shelf_id": "S_04"}
    # Non-critical by NON_CRITICAL_ACTIONS, not by a skip list — a failed
    # reset must not take the first bed's move_shelf down with it.
    assert not first.skip_on_failure


def test_skips_beds_with_missing_keys():
    beds = [
        {"bed_key": "", "location_id": "loc"},      # missing bed_key
        {"bed_key": "101-2", "location_id": ""},     # missing location_id
        {"bed_key": "101-3", "location_id": "loc-3"},
    ]
    steps = build_patrol_steps(beds, shelf_id="S_04", mode="patrol")
    bio_scan_steps = [s for s in steps if s.action == StepAction.BIO_SCAN.value]
    assert [s.params["bed_key"] for s in bio_scan_steps] == ["101-3"]


def test_all_steps_pending_status():
    steps = build_patrol_steps(_beds(), shelf_id="S_04", mode="patrol")
    assert all(s.status == StepStatus.PENDING for s in steps)


def test_final_return_shelf_carries_shelf_id():
    steps = build_patrol_steps(_beds(), shelf_id="S_04", mode="patrol")
    last = steps[-1]
    assert last.action == StepAction.RETURN_SHELF.value
    assert last.params == {"shelf_id": "S_04"}
