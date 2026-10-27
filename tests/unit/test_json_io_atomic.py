"""save_json must not leave a truncated file behind when a write is cut short.

It used to open the target with "w" (truncate first) and dump into it; a power
cut mid-write left partial JSON that load_json silently read back as defaults,
resetting the site's settings. Now it writes a temp file and os.replace()s it.
"""
import json
import os

import utils.json_io as json_io
from utils.json_io import load_json, save_json


def test_round_trip(tmp_path):
    path = str(tmp_path / "sub" / "settings.json")
    data = {"robot_ip": "192.168.40.2:26400", "名稱": "板橋", "n": [1, 2]}

    assert save_json(path, data) is True
    assert load_json(path) == data
    assert os.listdir(tmp_path / "sub") == ["settings.json"]  # no temp left over


def test_overwrite_keeps_file_mode(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text("{}")
    os.chmod(path, 0o640)

    assert save_json(str(path), {"a": 1}) is True
    assert os.stat(path).st_mode & 0o777 == 0o640


def test_failure_mid_write_keeps_original(tmp_path, monkeypatch):
    path = tmp_path / "settings.json"
    original = {"robot_ip": "192.168.40.2:26400", "mqtt_enabled": True}
    path.write_text(json.dumps(original))

    real_dump = json.dump

    def _dump_then_die(obj, fp, **kw):
        fp.write('{"robot_ip": "192.16')  # partial write, then the power goes
        raise OSError("simulated power cut")

    monkeypatch.setattr(json_io.json, "dump", _dump_then_die)
    assert save_json(str(path), {"robot_ip": "new"}) is False
    monkeypatch.setattr(json_io.json, "dump", real_dump)

    assert json.loads(path.read_text()) == original
    assert os.listdir(tmp_path) == ["settings.json"]  # temp file cleaned up


def test_failure_at_replace_keeps_original(tmp_path, monkeypatch):
    path = tmp_path / "settings.json"
    path.write_text('{"keep": true}')

    def _boom(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(json_io.os, "replace", _boom)
    assert save_json(str(path), {"keep": False}) is False

    assert json.loads(path.read_text()) == {"keep": True}
    assert os.listdir(tmp_path) == ["settings.json"]


def test_new_file_follows_umask(tmp_path):
    """A new settings.json must get the mode open(..., 'w') would have given
    it (0o666 & ~umask), not a hard-coded 0644 (code-review #54). The umask
    is read once at import so a save never toggles the process-wide value."""
    import os

    target = tmp_path / "new.json"
    assert save_json(str(target), {"a": 1})
    assert os.stat(target).st_mode & 0o777 == 0o666 & ~json_io._UMASK


def test_save_bytes_round_trip_and_stale_tmp_sweep(tmp_path):
    from utils.json_io import save_bytes

    target = tmp_path / "map.png"
    stale = tmp_path / ".map.png.abc.tmp"
    stale.write_bytes(b"orphan")
    assert save_bytes(str(target), b"\x89PNG")
    assert target.read_bytes() == b"\x89PNG"
    assert not stale.exists()
    assert not list(tmp_path.glob(".map.png.*.tmp"))


def test_save_follows_symlink(tmp_path):
    real = tmp_path / "real.json"
    real.write_text('{"a": 0}')
    link = tmp_path / "link.json"
    link.symlink_to(real)
    assert save_json(str(link), {"a": 1})
    assert link.is_symlink()
    assert json.loads(real.read_text()) == {"a": 1}
