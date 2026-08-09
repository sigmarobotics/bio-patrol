"""TODO-016: /api/maps/fetch must not silently unset the active map.

Local map ids are derived from robot_map_id and are stable across a refetch,
so the map the dashboard was showing comes back under the same id — clearing
active_map unconditionally left the dashboard blank until someone re-picked it
by hand. It stays cleared only when the robot no longer has that map.
"""
import asyncio

import pytest
from fastapi import HTTPException

import routers.maps as maps


class _FakeClient:
    def load_map_preview(self, robot_map_id):
        return robot_map_id  # stand-in protobuf; _save_map_png_and_meta is stubbed


class _FakeFleet:
    def __init__(self, robot_map_ids):
        self._ids = robot_map_ids

    async def get_map_list(self, robot_id):
        return {
            "maps": [{"id": mid, "name": mid} for mid in self._ids],
            "current_map_id": self._ids[0] if self._ids else "",
        }

    async def get_locations(self, robot_id):
        return {"ok": True, "locations": []}

    def get_raw_client(self, robot_id):
        return _FakeClient()


@pytest.fixture
def patched(monkeypatch, tmp_path):
    """Stub settings + persistence; returns a setter for the robot's map list."""
    import dependencies

    saved_settings = {"active_map": "map_a"}
    monkeypatch.setattr(maps, "MAPS_DIR", str(tmp_path))
    monkeypatch.setattr(maps, "get_runtime_settings", lambda: dict(saved_settings))
    monkeypatch.setattr(
        maps, "update_settings",
        lambda **kw: saved_settings.update(kw) or saved_settings,
    )
    monkeypatch.setattr(
        maps, "_save_map_png_and_meta",
        lambda map_pb, robot_map_id, locations, entry_name="": {
            "id": robot_map_id, "name": entry_name or robot_map_id,
            "timestamp": "t", "resolution": 0.05, "width": 1, "height": 1,
        },
    )

    def _set_robot_maps(ids):
        monkeypatch.setattr(dependencies, "get_fleet", lambda: _FakeFleet(ids))

    return _set_robot_maps, saved_settings


def test_active_map_restored_when_still_present(patched):
    set_robot_maps, saved_settings = patched
    set_robot_maps(["map_a", "map_b"])

    res = asyncio.run(maps.fetch_maps_from_robot())

    assert res["active_map"] == "map_a"
    assert saved_settings["active_map"] == "map_a"


def test_active_map_cleared_when_map_gone_from_robot(patched):
    set_robot_maps, saved_settings = patched
    set_robot_maps(["map_b"])

    res = asyncio.run(maps.fetch_maps_from_robot())

    assert res["active_map"] == ""
    assert saved_settings["active_map"] == ""


def test_no_maps_on_robot_reports_empty_active(patched):
    set_robot_maps, saved_settings = patched
    set_robot_maps([])

    res = asyncio.run(maps.fetch_maps_from_robot())

    assert res["active_map"] == ""
    assert saved_settings["active_map"] == ""


def test_unreachable_robot_leaves_local_maps_untouched(patched, tmp_path, monkeypatch):
    """Nothing local may be wiped before the robot has answered: a refetch
    against an offline or rebooting robot used to 502 having already deleted
    every local map and cleared active_map — the blank dashboard itself."""
    import dependencies

    _, saved_settings = patched
    (tmp_path / "map_a.json").write_text("{}")
    (tmp_path / "map_a.png").write_bytes(b"png")

    class _DeadFleet(_FakeFleet):
        async def get_map_list(self, robot_id):
            raise RuntimeError("robot unreachable")

    monkeypatch.setattr(dependencies, "get_fleet", lambda: _DeadFleet([]))

    with pytest.raises(HTTPException) as exc:
        asyncio.run(maps.fetch_maps_from_robot())

    assert exc.value.status_code == 502
    assert saved_settings["active_map"] == "map_a"
    assert (tmp_path / "map_a.json").exists()
    assert (tmp_path / "map_a.png").exists()
