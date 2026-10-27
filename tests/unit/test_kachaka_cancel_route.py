"""F-12 backend piece: POST /kachaka/{robot_id}/command/cancel sends the
robot-level cancel_command — no task state change, no cleanup queued."""
from fastapi import FastAPI
from fastapi.testclient import TestClient

import routers.kachaka as kachaka_router
from dependencies import get_fleet
from services.fleet_api import RobotNotRegistered


class _Fleet:
    def __init__(self, result=None, exc=None):
        self.calls = []
        self.result = result
        self.exc = exc

    async def cancel_command(self, robot_id):
        self.calls.append(robot_id)
        if self.exc:
            raise self.exc
        return self.result


def _client(fleet):
    app = FastAPI()
    app.include_router(kachaka_router.router)
    app.dependency_overrides[get_fleet] = lambda: fleet

    # Mirror main.py's global handler (pinned by test_kachaka_router_value_error).
    from fastapi.responses import JSONResponse

    @app.exception_handler(RobotNotRegistered)
    async def _h(_req, exc):
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    return TestClient(app)


def test_cancel_returns_the_fleet_result():
    result = {"ok": True, "error_code": 0, "cancelled_command": "return_home_command {}"}
    fleet = _Fleet(result=result)

    res = _client(fleet).post("/kachaka/kachaka/command/cancel")

    assert res.status_code == 200
    assert res.json() == result
    assert fleet.calls == ["kachaka"]


def test_cancel_passes_through_an_unreachable_robot_result():
    # kachaka_core's @with_retry turns a gRPC failure into ok=False, like the
    # sibling command routes.
    result = {"ok": False, "error": "UNAVAILABLE", "retryable": True}

    res = _client(_Fleet(result=result)).post("/kachaka/kachaka/command/cancel")

    assert res.status_code == 200
    assert res.json()["ok"] is False


def test_cancel_unknown_robot_is_404():
    fleet = _Fleet(exc=RobotNotRegistered("Robot ghost not registered"))

    res = _client(fleet).post("/kachaka/ghost/command/cancel")

    assert res.status_code == 404
