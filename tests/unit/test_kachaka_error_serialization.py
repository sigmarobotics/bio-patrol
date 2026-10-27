"""GET /kachaka/{id}/error and POST .../reset_shelf_pose returned HTTP 500
because protobuf objects reached FastAPI's jsonable_encoder (新營 traceback:
"cannot convert dictionary update sequence element #0 to a sequence")."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

from fastapi.encoders import jsonable_encoder
from kachaka_api.generated import kachaka_api_pb2 as pb2

import routers.kachaka as kachaka_router
from services.fleet_api import FleetAPI


def _fleet_with_queries(method: str, payload: dict) -> FleetAPI:
    fleet = FleetAPI()
    queries = MagicMock()
    getattr(queries, method).return_value = payload
    fleet._get_slot = lambda _rid: SimpleNamespace(queries=queries)
    return fleet


def _active_errors():
    # The exact container type the SDK's get_error() returns.
    return pb2.GetErrorResponse(error_codes=[21051, 21004]).error_codes


def test_get_errors_with_active_error_is_json_serialisable():
    fleet = _fleet_with_queries("get_errors", {"ok": True, "errors": _active_errors()})
    res = asyncio.run(fleet.get_errors("kachaka"))
    assert res == {"ok": True, "errors": [21051, 21004]}
    json.dumps(res)
    jsonable_encoder(res)


def test_get_errors_failure_dict_passes_through():
    fleet = _fleet_with_queries("get_errors", {"ok": False, "error": "timeout"})
    assert asyncio.run(fleet.get_errors("kachaka")) == {"ok": False, "error": "timeout"}


def test_get_status_with_active_error_is_json_serialisable():
    payload = {"ok": True, "pose": {"x": 0.0, "y": 0.0, "theta": 0.0}, "errors": _active_errors()}
    fleet = _fleet_with_queries("get_status", payload)
    res = asyncio.run(fleet.get_status("kachaka"))
    assert res["errors"] == [21051, 21004]
    json.dumps(res)
    jsonable_encoder(res)


def _reset_shelf_pose_route(result: pb2.Result) -> dict:
    client = MagicMock()
    client.reset_shelf_pose.return_value = result
    fleet = MagicMock()
    fleet.get_raw_client.return_value = client
    req = kachaka_router.ResetShelfPoseRequest(shelf_id="S01")
    return asyncio.run(kachaka_router.reset_shelf_pose("kachaka", req, fleet))


def test_reset_shelf_pose_success_is_json_serialisable():
    res = _reset_shelf_pose_route(pb2.Result(success=True))
    assert res == {"success": True, "error_code": 0}
    json.dumps(res)
    jsonable_encoder(res)


def test_reset_shelf_pose_failure_keeps_default_fields():
    # proto3 drops default values: without always-print a failed reset
    # (success=False, error_code=0) would come back as an empty {}.
    res = _reset_shelf_pose_route(pb2.Result(success=False, error_code=0))
    assert res == {"success": False, "error_code": 0}
    json.dumps(res)
