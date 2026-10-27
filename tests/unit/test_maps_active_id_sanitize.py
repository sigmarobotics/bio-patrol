"""/api/maps/active and /api/maps/switch take a local map id (a filename stem
under MAPS_DIR). Ids with path separators or '..' used to be joined straight
into a path — '../config/settings' was accepted and stored as active_map."""
import asyncio
import json

import pytest
from fastapi import HTTPException

import routers.maps as maps

BAD_IDS = ["../config/settings", "..", "a/b", "a\\b", "/etc/passwd", "", "x..y"]


@pytest.fixture
def patched(monkeypatch, tmp_path):
    saved_settings = {"active_map": "map_a"}
    maps_dir = tmp_path / "maps"
    maps_dir.mkdir()
    (maps_dir / "map_a.json").write_text(json.dumps({"robot_map_id": "map_a"}))
    (maps_dir / "6d1a0b3c-aa11-4bb2-9cc3-0123456789ab.json").write_text("{}")
    # A real .json one level up — what '../config/settings' would reach.
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "settings.json").write_text(json.dumps({"robot_map_id": "x"}))

    monkeypatch.setattr(maps, "MAPS_DIR", str(maps_dir))
    monkeypatch.setattr(
        maps, "update_settings",
        lambda **kw: saved_settings.update(kw) or saved_settings,
    )
    return saved_settings


@pytest.mark.parametrize("bad", BAD_IDS)
def test_set_active_rejects_unsafe_id(patched, bad):
    with pytest.raises(HTTPException) as exc:
        asyncio.run(maps.set_active_map(maps.SetActiveMapRequest(map_id=bad)))
    assert exc.value.status_code == 400
    assert patched["active_map"] == "map_a"


@pytest.mark.parametrize("bad", BAD_IDS)
def test_switch_rejects_unsafe_id(patched, bad):
    with pytest.raises(HTTPException) as exc:
        asyncio.run(maps.switch_map(maps.SwitchMapRequest(map_id=bad)))
    assert exc.value.status_code == 400
    assert patched["active_map"] == "map_a"


def test_set_active_accepts_uuid_map_id(patched):
    mid = "6d1a0b3c-aa11-4bb2-9cc3-0123456789ab"
    res = asyncio.run(maps.set_active_map(maps.SetActiveMapRequest(map_id=mid)))
    assert res == {"status": "ok", "active_map": mid}
    assert patched["active_map"] == mid


def test_set_active_unknown_valid_id_is_404(patched):
    with pytest.raises(HTTPException) as exc:
        asyncio.run(maps.set_active_map(maps.SetActiveMapRequest(map_id="nope")))
    assert exc.value.status_code == 404


def test_image_route_rejects_traversal(tmp_path, monkeypatch):
    monkeypatch.setattr(maps, "MAPS_DIR", str(tmp_path))
    with pytest.raises(HTTPException) as exc:
        asyncio.run(maps.get_map_image("../config/settings"))
    assert exc.value.status_code == 400


def test_active_info_ignores_traversal_in_settings(tmp_path, monkeypatch):
    monkeypatch.setattr(maps, "MAPS_DIR", str(tmp_path))
    monkeypatch.setattr(maps, "get_runtime_settings", lambda: {"active_map": "../config/settings"})
    with pytest.raises(HTTPException) as exc:
        asyncio.run(maps.get_active_map_info())
    assert exc.value.status_code == 404


def test_non_ascii_id_from_robot_is_accepted(patched, tmp_path):
    """_safe_map_id only rewrites path separators, so a robot map id with
    spaces or CJK must round-trip through the validator (code-review #54)."""
    (tmp_path / "maps" / "マップ 1.json").write_text("{}")
    res = asyncio.run(maps.set_active_map(maps.SetActiveMapRequest(map_id="マップ 1")))
    assert res["active_map"] == "マップ 1"
