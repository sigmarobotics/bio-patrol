"""CORNER-017 — Lifespan shutdown drains in-flight notifications.

Ground truth is the running `bio-patrol` container. Sends SIGTERM via
`docker compose stop -t 5` and asserts the shutdown logs flow through the
clean-up path that calls dispatcher.drain (3s timeout in main.py).

Skips cleanly when docker is not available locally.

The dispatcher's drain semantics (slow sinks complete before drain returns)
is pinned by tests/unit/test_notifications/test_dispatcher.py — this test
guards the wiring in main.py.lifespan instead.
"""
from __future__ import annotations

import ast
import re
import shutil
import subprocess
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
MAIN_PY = REPO_ROOT / "src" / "backend" / "main.py"


def _docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    r = subprocess.run(["docker", "compose", "ps", "--services"], cwd=REPO_ROOT,
                       capture_output=True, text=True, timeout=10)
    return r.returncode == 0 and "app" in r.stdout


@pytest.mark.hil
def test_lifespan_calls_dispatcher_drain_on_shutdown_via_ast():
    """AST walk: main.py shutdown path calls `dispatcher.drain(...)`.

    Cheap structural guard so a future refactor that drops the drain call
    fails this test instead of silently regressing the contract.
    """
    tree = ast.parse(MAIN_PY.read_text(encoding="utf-8"))
    found = False
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "drain"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "dispatcher"
        ):
            found = True
            break
    assert found, "main.py must call dispatcher.drain(...) during lifespan shutdown"


@pytest.mark.hil
def test_docker_stop_triggers_clean_shutdown():
    """SIGTERM via `docker compose stop -t 5` reaches the clean-up branch
    (drain + telegram client close + zigbee stop + scheduler stop). We
    assert the final 'Clean up completed' log line, which is emitted
    after every cleanup step has run."""
    if not _docker_available():
        pytest.skip("docker compose not available locally")

    # Make sure the container is up first.
    subprocess.run(["docker", "compose", "up", "-d", "app"], cwd=REPO_ROOT,
                   capture_output=True, text=True, timeout=60, check=True)

    # Stop with a generous timeout so the lifespan can complete.
    stop = subprocess.run(["docker", "compose", "stop", "-t", "5", "app"],
                          cwd=REPO_ROOT, capture_output=True, text=True, timeout=20)
    assert stop.returncode == 0, stop.stderr

    # Read recent logs and assert the clean-up message is present.
    logs = subprocess.run(["docker", "compose", "logs", "--tail", "30", "app"],
                          cwd=REPO_ROOT, capture_output=True, text=True, timeout=15)
    text = logs.stdout

    # Restart for the rest of the test session before any assertions can fail.
    subprocess.run(["docker", "compose", "up", "-d", "app"], cwd=REPO_ROOT,
                   capture_output=True, text=True, timeout=60)
    time.sleep(2)

    assert re.search(r"Application shutdown: Clean up completed\.", text), (
        f"Did not find clean-shutdown banner in last 30 log lines:\n{text[-2000:]}"
    )
    assert re.search(r"Application shutdown complete\.", text), (
        "uvicorn shutdown ack missing — lifespan likely hung"
    )
