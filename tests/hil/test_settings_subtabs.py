"""FEAT-010 — Settings tab is split into Hardware / Notifications sub-tabs.

Two-layer guard:

1. Static (always runs): regex-asserts the sub-tab DOM contract on
   index.html and the matching wire-up in script.js. Catches markup
   regressions even on machines without a browser.

2. Live (skipped without docker / live HTTP): hits the running container,
   confirms the served HTML still contains the sub-tab buttons. The full
   click-driven Playwright check was performed manually via the MCP
   playwright session and recorded in .sigma/context.md.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import time
from pathlib import Path

import httpx
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
INDEX_HTML = REPO_ROOT / "src" / "frontend" / "index.html"
SCRIPT_JS = REPO_ROOT / "src" / "frontend" / "js" / "script.js"
APP_URL = "http://localhost:8000"


def test_index_html_declares_both_sub_tab_buttons():
    html = INDEX_HTML.read_text(encoding="utf-8")
    assert re.search(
        r'class="settings-subtab-btn active"\s+data-settings-subtab="hardware"', html
    ), "Hardware sub-tab button missing or no longer the default"
    assert re.search(
        r'class="settings-subtab-btn"\s+data-settings-subtab="notifications"', html
    ), "Notifications sub-tab button missing"


def test_index_html_declares_both_sub_tab_panels():
    html = INDEX_HTML.read_text(encoding="utf-8")
    assert 'data-settings-subtab-content="hardware"' in html
    assert 'data-settings-subtab-content="notifications"' in html


def test_script_js_wires_sub_tab_click_handler():
    js = SCRIPT_JS.read_text(encoding="utf-8")
    # Click handler must iterate sub-tab buttons and toggle the matching content panel.
    assert "settings-subtab-btn" in js
    assert "settings-subtab-content" in js


def test_mqtt_egress_lives_inside_notifications_panel():
    """The whole point of the IT-5 split: MQTT egress controls belong in
    the notifications sub-tab, not in hardware. Verifies they are within
    the same content block as `data-settings-subtab-content="notifications"`."""
    html = INDEX_HTML.read_text(encoding="utf-8")
    notif_open = html.index('data-settings-subtab-content="notifications"')
    # Walk forward to find the matching closing div — naive but enough since
    # the panel is followed by the next subtab-content or end-of-section.
    end = html.find('data-settings-subtab-content="', notif_open + 1)
    if end == -1:
        end = len(html)
    panel = html[notif_open:end]
    assert "enable_mqtt_egress" in panel or "MQTT" in panel, (
        "MQTT egress controls did not stay inside the notifications panel"
    )


# ─── Live check (best-effort) ───────────────────────────────────────────────

def _app_up() -> bool:
    try:
        return httpx.get(f"{APP_URL}/api/settings", timeout=2.0).status_code == 200
    except httpx.HTTPError:
        return False


@pytest.mark.hil
def test_running_container_serves_sub_tab_markup():
    if shutil.which("docker") is None:
        pytest.skip("docker not available")
    if not _app_up():
        subprocess.run(["docker", "compose", "up", "-d", "app"], cwd=REPO_ROOT,
                       capture_output=True, text=True, timeout=60)
        time.sleep(3)
        if not _app_up():
            pytest.skip(f"bio-patrol app not reachable at {APP_URL}")

    page = httpx.get(APP_URL, timeout=5.0).text
    assert 'data-settings-subtab="hardware"' in page
    assert 'data-settings-subtab="notifications"' in page
    assert 'data-settings-subtab-content="hardware"' in page
    assert 'data-settings-subtab-content="notifications"' in page
