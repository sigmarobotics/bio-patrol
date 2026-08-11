"""TODO-019: /export_map and /import_map used to 500 on every call.

Both endpoints called the SDK with no arguments and fed the return value to
MessageToJson. The real signatures are ``export_map(map_id, output_file_path)
-> pb2.Result`` (streams into a file) and ``import_map(target_file_path,
chunk_size) -> (pb2.Result, map_id)`` (a tuple, not a message). HIL check on
the real robot is still pending.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from dependencies import get_fleet
from main import app


class _FakeResult:
    def __init__(self, success=True, error_code=0):
        self.success = success
        self.error_code = error_code


@pytest.fixture
def client():
    fake_fleet = MagicMock()
    app.dependency_overrides[get_fleet] = lambda: fake_fleet
    yield TestClient(app), fake_fleet
    app.dependency_overrides.clear()


def _sdk(export_result=None, import_result=None, current_map_id="map-current"):
    sdk = MagicMock()
    sdk.get_current_map_id = MagicMock(return_value=current_map_id)

    def _export(map_id, output_file_path):
        result = export_result or _FakeResult()
        if result.success:
            Path(output_file_path).write_bytes(b"MAPDATA:" + map_id.encode())
        return result

    sdk.export_map = MagicMock(side_effect=_export)
    sdk.import_map = MagicMock(
        return_value=(import_result or _FakeResult(), "map-imported")
    )
    return sdk


def test_export_map_streams_archive_bytes(client):
    c, fleet = client
    sdk = _sdk()
    fleet.get_raw_client.return_value = sdk

    r = c.get("/kachaka/kachaka/export_map")

    assert r.status_code == 200
    assert r.content == b"MAPDATA:map-current"
    assert "attachment" in r.headers["content-disposition"]
    # SDK called with both required args, defaulting to the current map.
    map_id, path = sdk.export_map.call_args[0]
    assert map_id == "map-current"
    assert path


def test_export_map_accepts_explicit_map_id(client):
    c, fleet = client
    sdk = _sdk()
    fleet.get_raw_client.return_value = sdk

    r = c.get("/kachaka/kachaka/export_map", params={"map_id": "map-7f"})

    assert r.status_code == 200
    assert r.content == b"MAPDATA:map-7f"
    assert sdk.get_current_map_id.call_count == 0


def test_export_map_reports_robot_failure_as_502(client):
    c, fleet = client
    fleet.get_raw_client.return_value = _sdk(
        export_result=_FakeResult(success=False, error_code=10501)
    )

    r = c.get("/kachaka/kachaka/export_map")

    assert r.status_code == 502
    assert "10501" in r.json()["detail"]


def test_export_map_filename_is_sanitized(client):
    c, fleet = client
    fleet.get_raw_client.return_value = _sdk()

    r = c.get("/kachaka/kachaka/export_map", params={"map_id": 'a"b/c\nd'})

    assert r.status_code == 200
    assert r.headers["content-disposition"] == 'attachment; filename="a_b_c_d.kmap"'


def test_import_map_uploads_file_and_returns_map_id(client):
    c, fleet = client
    sdk = _sdk()
    fleet.get_raw_client.return_value = sdk

    r = c.post(
        "/kachaka/kachaka/import_map",
        files={"file": ("floor7.kmap", b"MAPDATA", "application/octet-stream")},
    )

    assert r.status_code == 200
    assert r.json() == {"ok": True, "map_id": "map-imported"}
    # Payload actually reached the SDK through a real file path.
    (path,) = sdk.import_map.call_args[0]
    assert path


def test_import_map_reports_robot_failure_as_502(client):
    c, fleet = client
    fleet.get_raw_client.return_value = _sdk(
        import_result=_FakeResult(success=False, error_code=10502)
    )

    r = c.post(
        "/kachaka/kachaka/import_map",
        files={"file": ("floor7.kmap", b"MAPDATA", "application/octet-stream")},
    )

    assert r.status_code == 502
    assert "10502" in r.json()["detail"]
