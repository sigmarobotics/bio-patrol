"""CORNER-009 — mqtt-broker unreachable on startup → ZigbeeMQTT auto-reconnects.

ZigbeeMQTT runs in main.py's lifespan as a long-lived background task. If
mqtt-broker is down at app startup, the inner aiomqtt.Client raises
MqttError; ZigbeeMQTT._run catches it, sleeps with exponential backoff
(capped at 30s) and tries again. Once the broker is reachable, the next
attempt succeeds without any external poke.

This test stops `bio-patrol-mqtt`, restarts the app so it boots into a
no-broker world, observes at least one warning log, then brings the
broker back and asserts the eventual "Connected to MQTT" log line.
Skips when docker is unavailable.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


def _run(cmd: list[str], timeout: int = 30) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=REPO_ROOT, capture_output=True, text=True, timeout=timeout)


def _docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    r = _run(["docker", "compose", "ps", "--services"])
    return r.returncode == 0 and "mqtt-broker" in r.stdout and "app" in r.stdout


def _logs(service: str, tail: int = 80) -> str:
    return _run(["docker", "compose", "logs", "--tail", str(tail), service]).stdout


@pytest.mark.hil
@pytest.mark.slow
def test_mqtt_broker_unreachable_on_startup_then_reconnects():
    if not _docker_available():
        pytest.skip("docker compose not available locally")

    try:
        # 1. Stop the broker so app starts into a no-broker world.
        assert _run(["docker", "compose", "stop", "-t", "3", "mqtt-broker"]).returncode == 0

        # 2. Restart app — it will try to connect, fail, and start backoff.
        assert _run(["docker", "compose", "restart", "app"], timeout=60).returncode == 0
        time.sleep(8)  # give backoff at least one cycle

        # 3. Confirm app is alive but logged a connection failure.
        app_logs_during_outage = _logs("app", tail=60)
        assert re.search(r"MQTT.*(error|reconnecting|not connected)", app_logs_during_outage, re.IGNORECASE), (
            f"Expected an MQTT-error log during the broker outage, got:\n{app_logs_during_outage[-1500:]}"
        )

        # 4. Bring the broker back.
        assert _run(["docker", "compose", "up", "-d", "mqtt-broker"], timeout=30).returncode == 0
        time.sleep(8)  # exponential backoff caps at 30s; 8s is enough for the first 3 retries

        # 5. Assert ZigbeeMQTT eventually logs success.
        recovery_logs = _logs("app", tail=80)
        assert re.search(
            r"services\.zigbee_mqtt - INFO - Connected to MQTT mqtt-broker:1883",
            recovery_logs,
        ), f"ZigbeeMQTT did not log a successful reconnect:\n{recovery_logs[-1500:]}"
    finally:
        # Always restore both services so the rest of the suite has a working stack.
        _run(["docker", "compose", "up", "-d", "mqtt-broker", "app"], timeout=60)
        time.sleep(2)
