"""Shared pytest fixtures for bio-patrol HIL + unit tests."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ENV_TEST = Path.home() / ".claude" / ".env.test"
SETTINGS_JSON = PROJECT_ROOT / "data" / "config" / "settings.json"


def _load_env_test() -> dict[str, str]:
    if not ENV_TEST.exists():
        return {}
    out: dict[str, str] = {}
    for raw in ENV_TEST.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        out[key.strip()] = value.strip()
    return out


def _runtime_settings() -> dict:
    """Mirror of backend get_runtime_settings: defaults merged with settings.json."""
    from settings.defaults import DEFAULT_SETTINGS
    merged = dict(DEFAULT_SETTINGS)
    if SETTINGS_JSON.exists():
        try:
            merged.update(json.loads(SETTINGS_JSON.read_text()))
        except json.JSONDecodeError:
            pass
    return merged


def pytest_addoption(parser):
    parser.addoption(
        "--robot-ip",
        action="store",
        default="",
        help="Override robot IP (else read from settings.json / TEST_ROBOT_IP).",
    )


@pytest.fixture(scope="session")
def env_test() -> dict[str, str]:
    return _load_env_test()


@pytest.fixture(scope="session")
def runtime_settings() -> dict:
    return _runtime_settings()


@pytest.fixture
def robot_ip(request, env_test, runtime_settings) -> str:
    cli = request.config.getoption("--robot-ip")
    ip = cli or env_test.get("TEST_ROBOT_IP") or runtime_settings.get("robot_ip", "")
    if not ip:
        pytest.skip("Robot IP not provided (--robot-ip / TEST_ROBOT_IP / settings.json)")
    return ip


@pytest.fixture(scope="session")
def mqtt_settings(runtime_settings) -> dict:
    keys = ("mqtt_broker", "mqtt_port", "mqtt_topic")
    cfg = {k: runtime_settings.get(k) for k in keys}
    if not cfg["mqtt_broker"] or not cfg["mqtt_topic"]:
        pytest.skip("MQTT broker/topic not configured")
    return cfg
