"""IT-24 — announcer deploys a clip + the on-robot player over JupyterLab.

The robot side is faked at the robot_deployer level (_Jupyter); the real
``launch`` runs against the fake so its shell line is checked too.
"""
from __future__ import annotations

import asyncio
import io
import json
import threading
import urllib.error
import wave

import pytest

from services import announcer, robot_deployer
from services.sounds import sound_key

# Captured before the autouse fixture stubs it out.
_REAL_DEPLOY = announcer._deploy
_REAL_JUPYTER = robot_deployer._Jupyter

NAME = "sensor_silent_zh"
SECONDS = 1.0


def _wav_bytes() -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(24000)
        w.writeframes(b"\x00\x00" * int(24000 * SECONDS))
    return buf.getvalue()


CLIP = _wav_bytes()
CLIP_FILE = f"{sound_key(NAME, CLIP)}.wav"


class FakeJupyter:
    """Records every call; ``existing`` is what listdir(bio_sounds) returns."""

    instances: list["FakeJupyter"] = []
    existing: list[dict] = []
    login_error: Exception | None = None
    exec_out = "BIO-UP\r\n"
    exec_gate: threading.Event | None = None

    def __init__(self, robot_ip):
        self.robot_ip = robot_ip
        self.calls = []
        self.uploads = {}
        FakeJupyter.instances.append(self)

    def login(self):
        self.calls.append(("login", announcer._lock.locked()))
        if FakeJupyter.login_error:
            raise FakeJupyter.login_error

    def mkdir(self, path):
        self.calls.append(("mkdir", path))

    def listdir(self, path):
        self.calls.append(("listdir", path))
        return list(FakeJupyter.existing)

    def upload_binary(self, relpath, data):
        self.calls.append(("upload_binary", relpath))
        self.uploads[relpath] = data

    def upload(self, filename, content):
        self.calls.append(("upload", filename))
        self.uploads[filename] = content

    def exec(self, cmd, settle_s=2.0, stop_on=()):
        self.calls.append(("exec", cmd, stop_on))
        self.settle_s = settle_s
        if FakeJupyter.exec_gate:
            FakeJupyter.exec_gate.wait(5)
        return FakeJupyter.exec_out


@pytest.fixture(autouse=True)
def robot(monkeypatch, tmp_path):
    wav = tmp_path / f"{NAME}.wav"
    wav.write_bytes(CLIP)
    player = tmp_path / "bio_announce.py"
    player.write_text("# player\n", encoding="utf-8")
    monkeypatch.setattr(announcer, "_deploy", _REAL_DEPLOY)
    monkeypatch.setattr(announcer, "sound_path", lambda name: wav)
    monkeypatch.setattr(announcer, "PLAYER_SOURCE", player)
    monkeypatch.setattr(announcer, "_lock", asyncio.Lock())
    monkeypatch.setattr(announcer, "_tasks", set())
    monkeypatch.setattr(announcer, "_busy_until", {})
    monkeypatch.setattr("settings.config.get_runtime_settings",
                        lambda: {"robot_ip": "10.0.0.5:26400"})
    FakeJupyter.instances = []
    FakeJupyter.existing = []
    FakeJupyter.login_error = None
    FakeJupyter.exec_out = "BIO-UP\r\n"
    FakeJupyter.exec_gate = None
    monkeypatch.setattr(robot_deployer, "_Jupyter", FakeJupyter)
    return FakeJupyter


@pytest.fixture
def sleeps(monkeypatch):
    """Record asyncio.sleep durations (and whether the deploy lock was held)."""
    real = asyncio.sleep
    calls = []

    async def fake(delay, *a, **k):
        calls.append((delay, announcer._lock.locked()))
        await real(0)

    monkeypatch.setattr(announcer.asyncio, "sleep", fake)
    return calls


async def _drain():
    await asyncio.gather(*list(announcer._tasks))


@pytest.mark.asyncio
async def test_missing_clip_is_pushed_then_player_and_config_uploaded(robot, sleeps):
    assert await announcer.announce(NAME, wait=True) is True
    jp = robot.instances[0]
    assert jp.robot_ip == "10.0.0.5:26400"
    names = [c[0] for c in jp.calls]
    assert names[:3] == ["login", "mkdir", "listdir"]
    assert jp.uploads[f"bio_sounds/{CLIP_FILE}"] == CLIP
    assert jp.uploads["bio_announce.py"] == "# player\n"
    # The config is uploaded after the player, and launch comes last.
    assert names[-1] == "exec"
    assert jp.calls[-2][1].startswith("bio_announce_config-")


@pytest.mark.asyncio
async def test_clip_already_on_robot_is_not_pushed_again(robot, sleeps):
    robot.existing = [{"name": CLIP_FILE, "type": "file"}]
    assert await announcer.announce(NAME, wait=True) is True
    jp = robot.instances[0]
    assert not any(c[0] == "upload_binary" for c in jp.calls)
    assert "bio_announce.py" in jp.uploads


@pytest.mark.asyncio
async def test_stale_clip_with_other_hash_is_replaced(robot, sleeps):
    robot.existing = [{"name": f"{NAME}-deadbeef.wav", "type": "file"}]
    assert await announcer.announce(NAME, wait=True) is True
    assert f"bio_sounds/{CLIP_FILE}" in robot.instances[0].uploads


@pytest.mark.asyncio
async def test_config_contents(robot, sleeps):
    assert await announcer.announce(NAME, pre_delay_s=1.0, wait=True) is True
    jp = robot.instances[0]
    cfg_name = next(k for k in jp.uploads if k.startswith("bio_announce_config-"))
    rid = cfg_name[len("bio_announce_config-"):-len(".json")]
    assert json.loads(jp.uploads[cfg_name]) == {
        "sound_base": NAME,
        "sound_file": f"/home/kachaka/bio_sounds/{CLIP_FILE}",
        "seconds": SECONDS,
        "volume": 10,
        "pre_delay_s": 1.0,
    }
    # Launch uses the same run id as the config file.
    cmd = jp.calls[-1][1]
    assert f"/home/kachaka/bio_announce.py {rid} " in cmd
    assert f">> /tmp/bio_announce-{rid}.log" in cmd


@pytest.mark.asyncio
async def test_launch_line_is_detached_anchored_and_prunes_logs(robot, sleeps):
    assert await announcer.announce(NAME, wait=True) is True
    _, cmd, stop_on = robot.instances[0].calls[-1]
    rid = cmd.split("/home/kachaka/bio_announce.py ")[1].split(" ")[0]
    assert cmd.startswith("setsid nohup python3 -u ")
    assert "< /dev/null &" in cmd
    assert f'pgrep -f "bio_announce[.]py {rid.replace(".", "[.]")}( |$)"' in cmd
    assert "tail -n +11" in cmd
    # Split markers: the terminal's echo of the line never reads as a result.
    assert 'BIO-"UP"' in cmd and "BIO-UP" not in cmd
    assert stop_on == ("BIO-UP", "BIO-DOWN")


@pytest.mark.asyncio
async def test_launch_confirms_on_playback_not_on_spawn(robot, sleeps):
    """UP only once the player logged BIO-PLAYING; a player that dies first is
    DOWN. The window covers a clip still playing ahead on the robot's flock."""
    assert await announcer.announce(NAME, wait=True) is True
    jp = robot.instances[0]
    _, cmd, _ = jp.calls[-1]
    rid = cmd.split("/home/kachaka/bio_announce.py ")[1].split(" ")[0]
    log = f"/tmp/bio_announce-{rid}.log"
    assert f"grep -qsF BIO-PLAYING {log} && break; pgrep" in cmd
    assert f'if grep -qsF BIO-PLAYING {log}; then echo BIO-"UP"' in cmd
    # A DOWN run is stopped, so it cannot play after the Pi gave up on it.
    pattern = f'bio_announce[.]py {rid.replace(".", "[.]")}( |$)'
    assert f'else pkill -TERM -f "{pattern}"; echo BIO-"DOWN"' in cmd
    assert jp.settle_s >= 45


@pytest.mark.asyncio
async def test_launch_prunes_orphan_configs(robot, sleeps):
    """A config whose launch failed is never read (and so never deleted) by a player."""
    assert await announcer.announce(NAME, wait=True) is True
    cmd = robot.instances[0].calls[-1][1]
    assert ("find /home/kachaka -maxdepth 1 -name 'bio_announce_config-*.json' "
            "-mmin +10 -delete") in cmd


@pytest.mark.asyncio
async def test_unreachable_robot_returns_false_without_raising(robot, sleeps):
    robot.login_error = urllib.error.URLError("timed out")
    assert await announcer.announce(NAME, wait=True) is False
    assert sleeps == []
    # Background mode: the call itself reports scheduled; the task resolves False.
    assert await announcer.announce(NAME) is True
    results = await asyncio.gather(*list(announcer._tasks))
    assert results == [False]


@pytest.mark.asyncio
async def test_missing_clip_file_returns_false(robot, monkeypatch, tmp_path):
    monkeypatch.setattr(announcer, "sound_path", lambda name: tmp_path / "nope.wav")
    assert await announcer.announce(NAME, wait=True) is False
    assert robot.instances == []


@pytest.mark.asyncio
async def test_wait_sleeps_clip_length_after_success_outside_lock(robot, sleeps):
    assert await announcer.announce(NAME, wait=True) is True
    assert sleeps == [(SECONDS + 0.5, False)]
    # ...while the deploy itself ran under the lock.
    assert robot.instances[0].calls[0] == ("login", True)


@pytest.mark.asyncio
async def test_wait_returns_false_at_once_when_player_does_not_start(robot, sleeps):
    robot.exec_out = "BIO-DOWN\r\n"
    assert await announcer.announce(NAME, wait=True) is False
    assert sleeps == []


@pytest.mark.asyncio
async def test_no_wait_returns_immediately_and_task_is_retained(robot):
    robot.exec_gate = threading.Event()
    assert await announcer.announce(NAME) is True
    assert len(announcer._tasks) == 1
    task = next(iter(announcer._tasks))
    assert not task.done()
    robot.exec_gate.set()
    assert await task is True
    await asyncio.sleep(0)  # let the done callback run
    assert announcer._tasks == set()


@pytest.mark.asyncio
async def test_deploys_are_serialized(robot):
    robot.exec_gate = threading.Event()
    await announcer.announce(NAME)
    await announcer.announce(announcer.SOUND_BATTERY_LOW)  # same clip would coalesce
    for _ in range(50):
        if robot.instances:
            break
        await asyncio.sleep(0.01)
    await asyncio.sleep(0.05)
    # Second deploy waits for the first to release the lock.
    assert len(robot.instances) == 1
    robot.exec_gate.set()
    await _drain()
    assert len(robot.instances) == 2


@pytest.mark.asyncio
async def test_repeat_while_pending_or_playing_is_coalesced(robot, monkeypatch):
    """Two button presses seconds apart: one clip, not two back to back."""
    robot.exec_gate = threading.Event()
    assert await announcer.announce(NAME) is True
    assert await announcer.announce(NAME) is True   # deploy still in flight
    assert len(announcer._tasks) == 1
    robot.exec_gate.set()
    await _drain()
    assert await announcer.announce(NAME) is True   # clip still playing
    assert len(announcer._tasks) == 0
    # Once the clip is over, the same clip plays again.
    now = announcer.time.monotonic()
    monkeypatch.setattr(announcer.time, "monotonic", lambda: now + SECONDS + 1)
    await announcer.announce(NAME)
    await _drain()
    assert len(robot.instances) == 2


@pytest.mark.asyncio
async def test_failed_deploy_does_not_block_a_retry(robot):
    robot.exec_out = "BIO-DOWN\r\n"
    await announcer.announce(NAME)
    await _drain()
    await announcer.announce(NAME)
    await _drain()
    assert len(robot.instances) == 2


def test_safe_run_id_strips_shell_chars():
    assert robot_deployer.safe_run_id("a b;rm -rf/$(x)") == "a-b-rm--rf---x-"
    assert robot_deployer.safe_run_id("20261019-1.ab_c") == "20261019-1.ab_c"


def test_listdir_missing_directory_is_empty(monkeypatch):
    jp = _REAL_JUPYTER("10.0.0.5:26400")

    def fail(*a, **k):
        raise urllib.error.HTTPError("u", 404, "Not Found", {}, None)

    monkeypatch.setattr(jp, "_req", fail, raising=False)
    assert jp.listdir("bio_sounds") == []
