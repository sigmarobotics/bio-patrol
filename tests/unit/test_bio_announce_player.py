"""IT-24 — on-robot announcement player (assets/robot/bio_announce.py).

The player runs on the robot under Python 3.9, so it is loaded here from its
file path (never imported as a backend module) and driven through
``run(cfg, client, exit_fn)`` with a fake gRPC stub.
"""
from __future__ import annotations

import ast
import hashlib
import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

PLAYER = Path(__file__).resolve().parents[2] / "src" / "backend" / "assets" / "robot" / "bio_announce.py"
CLIP = b"RIFFfake-wav-bytes"
CLIP_NAME = "sensor_silent_zh-" + hashlib.sha256(CLIP).hexdigest()[:8]


def _ok():
    return SimpleNamespace(success=True, error_code=0)


def _fail():
    return SimpleNamespace(success=False, error_code=10001)


class FakeStub:
    def __init__(self, volume=0, sounds=(), get_raises=False, play_ok=True):
        self.volume = volume
        self.sounds = [SimpleNamespace(id=i, name=n) for i, n in sounds]
        self.get_raises = get_raises
        self.play_ok = play_ok
        self.calls = []          # (rpc, arg)
        self.timeouts = []
        self.on_play = None

    def GetSpeakerVolume(self, req, timeout=None):
        self.timeouts.append(timeout)
        self.calls.append(("get_volume", None))
        if self.get_raises:
            raise RuntimeError("unavailable")
        return SimpleNamespace(volume=self.volume)

    def SetSpeakerVolume(self, req, timeout=None):
        self.timeouts.append(timeout)
        self.calls.append(("set_volume", req.volume))
        self.volume = req.volume
        return SimpleNamespace(result=_ok())

    def GetSoundList(self, req, timeout=None):
        self.timeouts.append(timeout)
        self.calls.append(("list_sounds", None))
        return SimpleNamespace(sounds=self.sounds)

    def AddSound(self, req, timeout=None):
        self.timeouts.append(timeout)
        self.calls.append(("add_sound", req.name))
        return SimpleNamespace(result=_ok(), sound_id="new-id")

    def PlaySound(self, req, timeout=None):
        self.timeouts.append(timeout)
        self.calls.append(("play", req.sound_id))
        if self.on_play:
            self.on_play()
        return SimpleNamespace(result=_ok() if self.play_ok else _fail())

    def volumes_set(self):
        return [a for c, a in self.calls if c == "set_volume"]


@pytest.fixture
def player(tmp_path, monkeypatch):
    """Fresh module per test, paths in tmp_path, no real alarm or sleep."""
    spec = importlib.util.spec_from_file_location("bio_announce_under_test", PLAYER)
    mod = importlib.util.module_from_spec(spec)
    monkeypatch.setattr(sys, "dont_write_bytecode", True)  # keep assets/robot/ clean
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "RESIDUE_PATH", str(tmp_path / "restore.json"))
    monkeypatch.setattr(mod, "LOCK_PATH", str(tmp_path / "announce.lock"))
    monkeypatch.setattr(mod, "CONFIG_PATH_TMPL", str(tmp_path / "config-{}.json"))
    mod.events = []
    # A real alarm would SIGALRM-kill pytest ~a minute later: the fake exit_fn
    # does not end the process.
    monkeypatch.setattr(mod.signal, "alarm", lambda s: mod.events.append(("alarm", s)))
    monkeypatch.setattr(mod.time, "sleep", lambda s: mod.events.append(("sleep", s)))
    return mod


@pytest.fixture
def cfg(tmp_path):
    wav = tmp_path / "sensor_silent_zh-abc.wav"
    wav.write_bytes(CLIP)
    return {"sound_base": "sensor_silent_zh", "sound_file": str(wav),
            "seconds": 7.5, "volume": 10, "pre_delay_s": 0.0}


def _client(stub):
    return SimpleNamespace(stub=stub)


def _run(player, cfg, stub):
    exits = []
    player.run(cfg, _client(stub), exit_fn=exits.append)
    return exits


def test_volume_raised_then_restored(player, cfg):
    stub = FakeStub(volume=0)
    exits = _run(player, cfg, stub)

    assert stub.volumes_set() == [10, 0]
    assert stub.volume == 0
    assert ("play", "new-id") in stub.calls
    assert ("add_sound", CLIP_NAME) in stub.calls
    assert ("sleep", 8.0) in player.events
    assert set(stub.timeouts) == {5}
    assert not Path(player.RESIDUE_PATH).exists()
    assert exits == [0]


def test_existing_sound_reused(player, cfg):
    stub = FakeStub(volume=3, sounds=[("old-id", CLIP_NAME)])
    _run(player, cfg, stub)

    assert not any(c == "add_sound" for c, _ in stub.calls)
    assert ("play", "old-id") in stub.calls


def test_pre_delay_sleeps_after_raise_before_play(player, cfg):
    cfg["pre_delay_s"] = 1.0
    stub = FakeStub(volume=3)
    stub.on_play = lambda: player.events.append(("play",))
    _run(player, cfg, stub)

    assert player.events.index(("sleep", 1.0)) < player.events.index(("play",))


def test_residue_wins_over_current_volume(player, cfg):
    # A previous run was killed at volume 10; the robot's own volume was 4.
    Path(player.RESIDUE_PATH).write_text(json.dumps({"volume": 4}))
    stub = FakeStub(volume=10)
    exits = _run(player, cfg, stub)

    assert stub.volumes_set() == [10, 4]
    assert not Path(player.RESIDUE_PATH).exists()
    assert exits == [0]


def test_stale_residue_loses_to_volume_set_since(player, cfg):
    # The killed run left 3 behind, then staff set 6 by hand: 6 is the original.
    Path(player.RESIDUE_PATH).write_text(json.dumps({"volume": 3}))
    stub = FakeStub(volume=6)
    exits = _run(player, cfg, stub)

    assert stub.volumes_set() == [10, 6]
    assert not Path(player.RESIDUE_PATH).exists()
    assert exits == [0]


def test_residue_write_is_fsynced_before_rename(player, monkeypatch):
    order = []
    real_replace = player.os.replace
    monkeypatch.setattr(player.os, "fsync", lambda fd: order.append("fsync"))
    monkeypatch.setattr(player.os, "replace",
                        lambda a, b: (order.append("replace"), real_replace(a, b)))
    player._write_residue(3)

    # File data before the rename, the directory entry after it.
    assert order == ["fsync", "replace", "fsync"]
    assert json.loads(Path(player.RESIDUE_PATH).read_text()) == {"volume": 3}


def test_unreadable_volume_plays_nothing(player, cfg):
    stub = FakeStub(get_raises=True)
    exits = _run(player, cfg, stub)

    assert stub.volumes_set() == []
    assert not any(c == "play" for c, _ in stub.calls)
    assert not Path(player.RESIDUE_PATH).exists()
    assert exits == [1]


def test_play_failure_still_restores(player, cfg):
    stub = FakeStub(volume=2, play_ok=False)
    exits = _run(player, cfg, stub)

    assert stub.volumes_set() == [10, 2]
    assert not Path(player.RESIDUE_PATH).exists()
    assert exits == [1]


def test_playing_marker_logged_once_play_succeeds(player, cfg, capsys):
    # robot_deployer.launch confirms a run on this marker in its log.
    _run(player, cfg, FakeStub(volume=2))
    assert "BIO-PLAYING" in capsys.readouterr().out


def test_no_playing_marker_when_play_is_rejected(player, cfg, capsys):
    _run(player, cfg, FakeStub(volume=2, play_ok=False))
    assert "BIO-PLAYING" not in capsys.readouterr().out


def test_restore_retries_and_keeps_residue_on_failure(player, cfg):
    stub = FakeStub(volume=2)
    real_set = stub.SetSpeakerVolume

    def set_volume(req, timeout=None):
        if req.volume == 2:
            stub.calls.append(("set_volume", req.volume))
            raise RuntimeError("unavailable")
        return real_set(req, timeout=timeout)

    stub.SetSpeakerVolume = set_volume
    _run(player, cfg, stub)

    assert stub.volumes_set() == [10, 2, 2, 2]
    assert json.loads(Path(player.RESIDUE_PATH).read_text()) == {"volume": 2}


def test_sigterm_mid_play_restores_and_exits(player, cfg):
    stub = FakeStub(volume=3)
    exits = []

    class Exited(BaseException):  # like os._exit, not caught by `except Exception`
        pass

    def exit_fn(code):
        exits.append(code)
        raise Exited  # os._exit never returns

    def sigterm():
        stub.calls.append(("sigterm", None))
        player._on_sigterm(15, None)

    stub.on_play = sigterm
    with pytest.raises(Exited):
        player.run(cfg, _client(stub), exit_fn=exit_fn)

    sig = stub.calls.index(("sigterm", None))
    assert ("set_volume", 3) in stub.calls[sig:]
    assert exits[0] == 0
    assert not Path(player.RESIDUE_PATH).exists()


def test_sigterm_before_volume_change_does_not_touch_volume(player):
    stub = FakeStub(volume=3)
    exits = []
    player._state.update(client=_client(stub), exit_fn=exits.append)

    player._on_sigterm(15, None)
    player._on_sigterm(15, None)  # repeat is ignored

    assert stub.volumes_set() == []
    assert exits == [0]


def test_alarm_armed_only_after_lock(player, cfg, monkeypatch):
    real_flock = player.fcntl.flock

    def flock(fd, op):
        player.events.append(("flock",))
        return real_flock(fd, op)

    monkeypatch.setattr(player.fcntl, "flock", flock)
    _run(player, cfg, FakeStub(volume=0))

    assert player.events[:2] == [("flock",), ("alarm", 67)]


def test_config_deleted_after_read(player, tmp_path):
    path = tmp_path / "config-r1.json"
    path.write_text(json.dumps({"sound_base": "x", "seconds": 1}))

    assert player.load_config("r1") == {"sound_base": "x", "seconds": 1}
    assert not path.exists()


def test_source_parses_as_python39():
    ast.parse(PLAYER.read_text(), feature_version=(3, 9))


def test_source_never_uses_kachaka_core():
    src = PLAYER.read_text()
    assert "kachaka_core" not in src
    assert "SIGALRM" not in src  # the alarm is a hard deadline: no handler


def _python39():
    uv = shutil.which("uv")
    if not uv:
        return None
    found = subprocess.run([uv, "python", "find", "3.9"], capture_output=True, text=True)
    py = found.stdout.strip()
    if found.returncode != 0 or not py:
        return None
    ver = subprocess.run([py, "-c", "import sys; print(sys.version_info[:2])"],
                         capture_output=True, text=True)
    return py if ver.stdout.strip() == "(3, 9)" else None


def test_py_compile_under_python39(tmp_path):
    py = _python39()
    if py is None:
        pytest.skip("no Python 3.9 interpreter found")
    out = subprocess.run(
        [py, "-c", "import py_compile, sys; py_compile.compile(sys.argv[1], cfile=sys.argv[2], doraise=True)",
         str(PLAYER), str(tmp_path / "bio_announce.pyc")],
        capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
