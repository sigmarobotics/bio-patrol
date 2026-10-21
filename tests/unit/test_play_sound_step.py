"""IT-19 — arrival voice.

FleetAPI.play_sound_by_name uploads a bundled clip once per content hash and
then plays it by id. Since IT-24 the play_sound step goes through the
on-robot announcer instead; the step is advisory, so a clip that does not play
still leaves the bed scanned.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from common_types import StepAction, StepStatus, Task, TaskStatus, TaskStep
from services import task_runtime
from services.fleet_api import FleetAPI
from services.sounds import sound_key

CLIP = b"RIFFfake-wav-bytes"


def _wire_mocks(monkeypatch):
    fake_conn = MagicMock()
    fake_conn.ping.return_value = {"ok": True, "serial": "BKP"}
    fake_conn.serial = "BKP"
    monkeypatch.setattr(
        "services.fleet_api.KachakaConnection",
        MagicMock(get=MagicMock(return_value=fake_conn), remove=MagicMock()),
    )
    monkeypatch.setattr(
        "services.fleet_api.RobotController", MagicMock(return_value=MagicMock())
    )
    fake_cmds = MagicMock()
    monkeypatch.setattr(
        "services.fleet_api.KachakaCommands", MagicMock(return_value=fake_cmds)
    )
    fake_queries = MagicMock()
    monkeypatch.setattr(
        "services.fleet_api.KachakaQueries", MagicMock(return_value=fake_queries)
    )
    return fake_cmds, fake_queries


@pytest.fixture
def clip(tmp_path, monkeypatch):
    """Point the fleet at a throwaway WAV so the key is stable and local."""
    path = tmp_path / "arrival_zh.wav"
    path.write_bytes(CLIP)
    monkeypatch.setattr("services.fleet_api.sound_path", lambda name: path)
    return sound_key("arrival_zh", CLIP)


@pytest.mark.asyncio
async def test_existing_sound_on_robot_is_played_without_reuploading(monkeypatch, clip):
    fake_cmds, fake_queries = _wire_mocks(monkeypatch)
    fake_queries.list_sounds.return_value = {
        "ok": True, "sounds": [{"id": "snd-1", "name": clip}],
    }
    fake_cmds.play_sound.return_value = {"ok": True}

    fleet = FleetAPI()
    await fleet.register_robot("kachaka", "1.2.3.4")
    result = await fleet.play_sound_by_name("kachaka", "arrival_zh")

    assert result == {"ok": True}
    fake_cmds.add_sound.assert_not_called()
    fake_cmds.play_sound.assert_called_once_with("snd-1")


@pytest.mark.asyncio
async def test_missing_sound_is_uploaded_once_then_cached(monkeypatch, clip):
    """The upload id is authoritative — list_sounds is eventually consistent,
    so a second call must reuse the cached id, not re-query."""
    fake_cmds, fake_queries = _wire_mocks(monkeypatch)
    fake_queries.list_sounds.return_value = {"ok": True, "sounds": []}
    fake_cmds.add_sound.return_value = {"ok": True, "sound_id": "snd-new"}
    fake_cmds.play_sound.return_value = {"ok": True}

    fleet = FleetAPI()
    await fleet.register_robot("kachaka", "1.2.3.4")
    await fleet.play_sound_by_name("kachaka", "arrival_zh")
    await fleet.play_sound_by_name("kachaka", "arrival_zh")

    fake_cmds.add_sound.assert_called_once_with(clip, data=CLIP)
    fake_queries.list_sounds.assert_called_once()
    assert fake_cmds.play_sound.call_args_list == [
        (("snd-new",),), (("snd-new",),),
    ]


@pytest.mark.asyncio
async def test_stale_clip_of_the_same_name_is_left_alone(monkeypatch, clip):
    """A re-recorded WAV gets a new content key. The old upload stays on the
    robot — deleting sounds is not ours to do."""
    fake_cmds, fake_queries = _wire_mocks(monkeypatch)
    fake_queries.list_sounds.return_value = {
        "ok": True, "sounds": [{"id": "snd-old", "name": "arrival_zh-deadbeef"}],
    }
    fake_cmds.add_sound.return_value = {"ok": True, "sound_id": "snd-new"}
    fake_cmds.play_sound.return_value = {"ok": True}

    fleet = FleetAPI()
    await fleet.register_robot("kachaka", "1.2.3.4")
    await fleet.play_sound_by_name("kachaka", "arrival_zh")

    fake_cmds.add_sound.assert_called_once_with(clip, data=CLIP)
    fake_cmds.delete_sound.assert_not_called()
    fake_cmds.play_sound.assert_called_once_with("snd-new")


@pytest.mark.asyncio
async def test_failed_upload_is_returned_and_nothing_is_played(monkeypatch, clip):
    fake_cmds, fake_queries = _wire_mocks(monkeypatch)
    fake_queries.list_sounds.return_value = {"ok": True, "sounds": []}
    fake_cmds.add_sound.return_value = {"ok": False, "error": "STORAGE_FULL"}

    fleet = FleetAPI()
    await fleet.register_robot("kachaka", "1.2.3.4")
    result = await fleet.play_sound_by_name("kachaka", "arrival_zh")

    assert result == {"ok": False, "error": "STORAGE_FULL"}
    fake_cmds.play_sound.assert_not_called()


# ── step handler ─────────────────────────────────────────────────────────────

def _fleet() -> MagicMock:
    fleet = MagicMock()
    fleet.get_shelves = AsyncMock(return_value={"ok": True, "shelves": []})
    fleet.get_locations = AsyncMock(return_value={"ok": True, "locations": []})
    fleet.get_metrics = AsyncMock(return_value={
        "poll_count": 0, "poll_rtt_list": [], "poll_success_count": 0,
    })
    fleet.reset_metrics = AsyncMock(return_value=None)
    return fleet


def _voice_task(params):
    return Task(
        task_id="t-voice",
        robot_id="kachaka",
        steps=[
            TaskStep(
                step_id="voice_0",
                action=StepAction.PLAY_SOUND.value,
                params=params,
                status=StepStatus.PENDING,
            ),
            TaskStep(
                step_id="action_0",
                action=StepAction.WAIT.value,
                params={"seconds": "0"},
                status=StepStatus.PENDING,
            ),
        ],
        status=TaskStatus.QUEUED,
    )


def _run(task):
    engine = task_runtime.TaskEngine(_fleet(), "kachaka")
    with patch.object(task_runtime.dispatcher, "dispatch", new=AsyncMock(return_value=None)):
        return asyncio.run(engine.run_task(task))


def test_play_sound_failure_does_not_stop_the_patrol(monkeypatch):
    """A clip that did not play (deploy failed, robot JupyterLab down) fails
    the step. The greeting is a nicety; the scan behind it is the reason the
    robot drove there."""
    async def _not_played(name, *, pre_delay_s=0.0, wait=False):
        return False

    monkeypatch.setattr(task_runtime.announcer, "announce", _not_played)
    task = _voice_task({"sound_name": "arrival_zh"})

    result = _run(task)

    assert result.status != TaskStatus.FAILED
    assert task.steps[0].status == StepStatus.FAIL
    assert task.steps[1].status == StepStatus.SUCCESS


def test_patrol_voice_step_goes_through_the_announcer_without_waiting(announced):
    """IT-24: playback runs on the robot (volume 10, then restore)."""
    task = _voice_task({"sound_name": "arrival_zh"})

    _run(task)

    assert announced == [("arrival_zh", 0.0, False)]
    assert task.steps[0].status == StepStatus.SUCCESS


def test_demo_voice_step_waits_for_the_clip(announced):
    task = _voice_task({"sound_name": "arrival_zh", "wait": True})

    _run(task)

    assert announced == [("arrival_zh", 0.0, True)]
    assert task.steps[0].status == StepStatus.SUCCESS


def test_shelf_drop_cuts_a_waiting_voice_step_short(monkeypatch):
    """A drop must not sit behind the clip: the wait races the drop event."""
    engine = task_runtime.TaskEngine(_fleet(), "kachaka")

    async def _slow(name, *, pre_delay_s=0.0, wait=False):
        engine.shelf_drop_event.set()
        await asyncio.sleep(30)
        return True

    monkeypatch.setattr(task_runtime.announcer, "announce", _slow)
    step = _voice_task({"sound_name": "arrival_zh", "wait": True}).steps[0]

    async def _go():
        engine.shelf_drop_event = asyncio.Event()
        engine._battery_abort_event = asyncio.Event()
        # the wait verifies the signal against the robot first: a real release
        engine._verify_shelf_still_docked = AsyncMock(return_value="released")
        return await asyncio.wait_for(engine._do_play_sound(step), 5)

    result = asyncio.run(_go())

    assert result.success is False
