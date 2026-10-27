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
