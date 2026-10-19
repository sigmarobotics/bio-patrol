"""Robot voice announcements played by an on-robot player (IT-24).

The Pi only uploads the clip, the player and a per-run config over the robot's
JupyterLab, then launches ``bio_announce.py`` there; the player raises the
speaker to volume 10, plays, and restores the robot's own volume over local
gRPC, so it finishes even if WiFi drops. A failed deploy is logged and
nothing plays — there is no fallback to playing over the Pi's gRPC link.
"""
import asyncio
import json
import logging
import math
import secrets
import time
from pathlib import Path

from services import robot_deployer
from services.sounds import sound_key, sound_path, wav_seconds

logger = logging.getLogger(__name__)

SOUND_SENSOR_OFFLINE = "sensor_pc_offline_zh"
SOUND_SENSOR_SILENT = "sensor_silent_zh"
SOUND_BATTERY_LOW = "battery_low_zh"

PLAYER_SOURCE = Path(__file__).resolve().parent.parent / "assets" / "robot" / "bio_announce.py"
SOUND_DIR_REMOTE = "bio_sounds"  # under /home/kachaka, the Jupyter root
VOLUME = 10

# Serializes deploys from this Pi; the player's flock on the robot is what
# actually keeps two clips from overlapping.
_lock = asyncio.Lock()
# Background deploys, held so they are not garbage-collected mid-flight.
_tasks: set[asyncio.Task] = set()
# Clip name -> monotonic time its playback ends (inf while the deploy is in
# flight): a repeated fire-and-forget request inside it is dropped, so two
# button presses seconds apart say the refusal once, not twice back to back.
_busy_until: dict[str, float] = {}


def _deploy(name: str, pre_delay_s: float) -> bool:
    """Push clip/player/config to the robot and launch the player (sync).

    True only once the player reports the clip playing on the robot.
    """
    from settings.config import get_runtime_settings

    path = sound_path(name)
    data = path.read_bytes()
    clip = f"{sound_key(name, data)}.wav"
    rid = robot_deployer.safe_run_id(
        f"{time.strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(3)}")
    config = {
        "sound_base": name,
        "sound_file": f"/home/kachaka/{SOUND_DIR_REMOTE}/{clip}",
        "seconds": wav_seconds(path),
        "volume": VOLUME,
        "pre_delay_s": pre_delay_s,
    }
    jp = robot_deployer._Jupyter(get_runtime_settings()["robot_ip"])
    jp.login()
    jp.mkdir(SOUND_DIR_REMOTE)
    # The name carries the content hash, so "already there" means "same clip".
    if clip not in {e.get("name") for e in jp.listdir(SOUND_DIR_REMOTE)}:
        jp.upload_binary(f"{SOUND_DIR_REMOTE}/{clip}", data)
    jp.upload(robot_deployer.PLAYER, PLAYER_SOURCE.read_text(encoding="utf-8"))
    jp.upload(f"bio_announce_config-{rid}.json", json.dumps(config))
    if not robot_deployer.launch(jp, rid):
        logger.warning("announce %s: player did not start on the robot (run %s)", name, rid)
        return False
    logger.info("announce %s: player started (run %s)", name, rid)
    return True


async def _run(name: str, pre_delay_s: float) -> bool:
    try:
        async with _lock:
            if not await asyncio.to_thread(_deploy, name, pre_delay_s):
                return False
        seconds = await asyncio.to_thread(wav_seconds, sound_path(name))
        _busy_until[name] = time.monotonic() + seconds + 0.5
        return True
    except Exception as e:
        logger.warning("announce %s: deploy failed: %s", name, e)
        return False
    finally:
        if _busy_until.get(name) == math.inf:
            del _busy_until[name]  # nothing plays: a retry must not be dropped


async def announce(name: str, *, pre_delay_s: float = 0.0, wait: bool = False) -> bool:
    """Play bundled clip ``name`` on the robot at volume 10, then restore the
    robot's own volume. Never raises; returns False when nothing was played.

    wait=False schedules the deploy and returns True at once — or drops the
    request (still True) while the same clip is being deployed or played.
    wait=True returns after the clip has played (playback confirmed + its
    duration + 0.5 s), or False right away if the deploy failed.
    """
    try:
        if not wait and _busy_until.get(name, 0.0) > time.monotonic():
            logger.info("announce %s: same clip already pending or playing, dropped", name)
            return True
        _busy_until[name] = math.inf
        task = asyncio.create_task(_run(name, pre_delay_s))
        _tasks.add(task)
        task.add_done_callback(_tasks.discard)
        if not wait:
            return True
        # Shielded: a caller that gives up waiting must not cancel a deploy
        # halfway (the player could be left uploaded but never launched).
        if not await asyncio.shield(task):
            return False
        seconds = await asyncio.to_thread(wav_seconds, sound_path(name))
        await asyncio.sleep(seconds + 0.5)
        return True
    except Exception as e:
        logger.warning("announce %s failed: %s", name, e)
        return False
