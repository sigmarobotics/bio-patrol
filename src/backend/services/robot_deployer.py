"""Robot-side deploy channel over the robot's JupyterLab (IT-24).

Ported (minimal subset) from kiosk ``src/backend/deployer.py`` @ 5bc1217:
upload files through the contents API and run shell lines through a terminal
websocket on :26501 (password ``kachaka``), so the Pi needs no ssh key. The
announcer uses it to launch ``bio_announce.py`` on the robot, which then plays
over local gRPC and finishes even if WiFi drops mid-clip.

All functions are sync; callers use ``asyncio.to_thread`` (``exec`` runs
``asyncio.run`` internally).
"""

import asyncio
import base64
import http.cookiejar
import json
import re
import urllib.error
import urllib.parse
import urllib.request

import websockets

JUPYTER_PASSWORD = "kachaka"  # Kachaka Playground factory default (public on the LAN)
PLAYER = "bio_announce.py"    # uploaded to /home/kachaka/, the Jupyter root
LOG_KEEP = 10                 # /tmp/bio_announce-*.log files kept (one per run)
PLAYING = "BIO-PLAYING"       # the player logs this once PlaySound succeeded
# Spawn → playback: up to a 20 s clip still playing ahead of it on the robot's
# flock, then the kachaka_api import (seconds), gRPC probe and sound RPCs.
CONFIRM_S = 45


def safe_run_id(raw: str) -> str:
    """run_id goes into file names and a shell line: keep [A-Za-z0-9._-]."""
    return re.sub(r"[^A-Za-z0-9._-]", "-", str(raw))[:64]


class _Jupyter:
    """Minimal JupyterLab REST/WS client (stdlib + websockets)."""

    def __init__(self, robot_ip: str):
        host = robot_ip.split(":")[0]
        self.base = f"http://{host}:26501"
        self.ws_base = f"ws://{host}:26501"
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar))

    def _xsrf(self) -> str:
        for c in self.jar:
            if c.name == "_xsrf":
                return c.value
        return ""

    def _cookies(self) -> str:
        return "; ".join(f"{c.name}={c.value}" for c in self.jar)

    def _req(self, method: str, path: str, data=None, headers=None, timeout=15):
        h = {"X-XSRFToken": self._xsrf()}
        if headers:
            h.update(headers)
        r = urllib.request.Request(self.base + path, data=data, headers=h, method=method)
        return self.opener.open(r, timeout=timeout)

    def login(self):
        self.opener.open(self.base + "/login", timeout=10).read()
        body = urllib.parse.urlencode(
            {"_xsrf": self._xsrf(), "password": JUPYTER_PASSWORD}).encode()
        self._req("POST", "/login", body,
                  {"Content-Type": "application/x-www-form-urlencoded"}).read()

    def upload(self, filename: str, content: str):
        body = json.dumps({"type": "file", "format": "text", "content": content}).encode()
        self._req("PUT", f"/api/contents/{urllib.parse.quote(filename)}", body,
                  {"Content-Type": "application/json"}).read()

    def upload_binary(self, relpath: str, data: bytes):
        """Upload a binary file (WAV) base64-encoded."""
        body = json.dumps({"type": "file", "format": "base64",
                           "content": base64.b64encode(data).decode()}).encode()
        self._req("PUT", f"/api/contents/{urllib.parse.quote(relpath)}", body,
                  {"Content-Type": "application/json"}).read()

    def mkdir(self, path: str):
        """Create a directory (succeeds if it already exists)."""
        body = json.dumps({"type": "directory"}).encode()
        self._req("PUT", f"/api/contents/{urllib.parse.quote(path)}", body,
                  {"Content-Type": "application/json"}).read()

    def listdir(self, path: str) -> list[dict]:
        """Entries (name/path/type) of a robot directory; [] if it does not exist."""
        try:
            raw = self._req(
                "GET", f"/api/contents/{urllib.parse.quote(path)}?content=1&type=directory"
            ).read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return []
            raise
        return json.loads(raw).get("content") or []

    async def _terminal_exec(self, cmd: str, settle_s: float = 2.0,
                             stop_on: tuple[str, ...] = ()) -> str:
        term = json.loads(self._req("POST", "/api/terminals", b"").read())
        name = term["name"]
        url = f"{self.ws_base}/terminals/websocket/{name}"
        headers = {"Cookie": self._cookies(), "Origin": self.base}
        out = []
        try:
            async with websockets.connect(url, additional_headers=headers,
                                          open_timeout=30, max_size=None) as ws:
                await asyncio.sleep(1.0)
                await ws.send(json.dumps(["stdin", cmd + "\n"]))
                deadline = asyncio.get_event_loop().time() + settle_s
                while asyncio.get_event_loop().time() < deadline:
                    try:
                        m = json.loads(await asyncio.wait_for(ws.recv(), timeout=0.5))
                        if m[0] == "stdout":
                            out.append(m[1])
                    except asyncio.TimeoutError:
                        continue
                    # Joined: a marker can straddle two stdout chunks.
                    text = "".join(out)
                    if any(s in text for s in stop_on):
                        break
        finally:
            try:
                self._req("DELETE", f"/api/terminals/{name}").read()
            except Exception:
                pass
        return "".join(out)

    def exec(self, cmd: str, settle_s: float = 2.0, stop_on: tuple[str, ...] = ()) -> str:
        """Run one shell line on the robot; return its output within settle_s
        seconds, or as soon as any ``stop_on`` marker shows up.

        Calls asyncio.run(): worker threads only (asyncio.to_thread).
        """
        return asyncio.run(self._terminal_exec(cmd, settle_s, stop_on))


def launch(jp: _Jupyter, run_id: str) -> bool:
    """Start ``bio_announce.py <run_id>`` and confirm the clip is playing, in
    one exec: UP once the run's log shows BIO-PLAYING, DOWN if the player
    exits first or CONFIRM_S runs out.

    setsid: terminado kills the terminal's process group on close (nohup does
    not survive that). pgrep is anchored to this run's argv and first waits
    for the process to appear (fork → exec); the result marker is split
    (BIO-"UP") so the terminal's echo of the line never matches. Configs older
    than 10 min belong to launches that never started and are pruned here.
    """
    rid = safe_run_id(run_id)
    pattern = f'{PLAYER.replace(".", "[.]")} {rid.replace(".", "[.]")}( |$)'
    log = f"/tmp/bio_announce-{rid}.log"
    out = jp.exec(
        f"setsid nohup python3 -u /home/kachaka/{PLAYER} {rid} "
        f">> {log} 2>&1 < /dev/null & "
        f"ls -1t /tmp/bio_announce-*.log 2>/dev/null | tail -n +{LOG_KEEP + 1} "
        f"| xargs -r rm -f; "
        f"find /home/kachaka -maxdepth 1 -name 'bio_announce_config-*.json' "
        f"-mmin +10 -delete; "
        f'for i in $(seq 1 10); do pgrep -f "{pattern}" >/dev/null && break; '
        f"sleep 0.2; done; "
        f"for i in $(seq 1 {CONFIRM_S * 2}); do grep -qsF {PLAYING} {log} && break; "
        f'pgrep -f "{pattern}" >/dev/null || break; sleep 0.5; done; '
        f'if grep -qsF {PLAYING} {log}; then echo BIO-"UP"; '
        f'else echo BIO-"DOWN"; fi',
        settle_s=CONFIRM_S + 5, stop_on=("BIO-UP", "BIO-DOWN"))
    return "BIO-UP" in out
