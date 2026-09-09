"""IT-19 — arrival voice.

FleetAPI.play_sound_by_name uploads a bundled clip once per content hash and
then plays it by id; the patrol step that calls it is advisory, so a robot
whose firmware has no Sound API still gets scanned.
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


def test_play_sound_failure_does_not_stop_the_patrol():
    """Firmware below 3.17 has no Sound API. The greeting is a nicety; the
    scan behind it is the reason the robot drove there."""
    fleet = _fleet()
    fleet.play_sound_by_name = AsyncMock(side_effect=RuntimeError("UNIMPLEMENTED"))
    engine = task_runtime.TaskEngine(fleet, "kachaka")
    task = Task(
        task_id="t-voice",
        robot_id="kachaka",
        steps=[
            TaskStep(
                step_id="voice_0",
                action=StepAction.PLAY_SOUND.value,
                params={"sound_name": "arrival_zh"},
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

    with patch.object(task_runtime.dispatcher, "dispatch", new=AsyncMock(return_value=None)):
        result = asyncio.run(engine.run_task(task))

    assert result.status != TaskStatus.FAILED
    assert task.steps[0].status == StepStatus.FAIL
    assert task.steps[0].result.error_message == "UNIMPLEMENTED"
    assert task.steps[1].status == StepStatus.SUCCESS
