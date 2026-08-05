"""Unit tests for the z2m config snapshot service (power-loss resilience)."""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import routers.buttons as buttons_router
from main import app
from services import zigbee_snapshot
from services.zigbee_snapshot import SnapshotService

STATUS_KEYS = {"enabled", "last_snapshot", "generations", "last_error", "last_restore"}


@pytest.fixture
def svc():
    """Configured service over a throwaway z2m data dir + snapshot dir."""
    with tempfile.TemporaryDirectory() as d:
        z2m = Path(d) / "z2m"
        z2m.mkdir()
        (z2m / "configuration.yaml").write_text("network_key: v1\n")
        (z2m / "database.db").write_text('{"id":1}\n')
        snap = Path(d) / "snapshots"
        s = SnapshotService()
        s.configure(str(z2m), str(snap))
        yield s, z2m, snap


def _gens(snap: Path) -> list[Path]:
    return sorted(
        (p for p in snap.glob("gen-*") if not p.name.endswith(".tmp")),
        key=lambda p: p.name,
    )


# ── take ────────────────────────────────────────────────────────────────


def test_take_creates_generation_with_meta(svc):
    s, z2m, snap = svc
    result = s.take("startup")

    assert result["reason"] == "startup"
    gens = _gens(snap)
    assert [g.name for g in gens] == [result["gen"]]

    gen = gens[0]
    assert (gen / "configuration.yaml").read_text() == "network_key: v1\n"
    assert (gen / "database.db").read_text() == '{"id":1}\n'
    # Absent source file is simply not part of the generation.
    assert not (gen / "coordinator_backup.json").exists()

    meta = json.loads((gen / "meta.json").read_text())
    assert meta["reason"] == "startup"
    assert set(meta["hashes"]) == {"configuration.yaml", "database.db"}
    assert isinstance(meta["ts"], int)


def test_identical_content_does_not_create_second_generation(svc):
    s, _z2m, snap = svc
    s.take("startup")
    assert s.take("devices_changed") is None
    assert len(_gens(snap)) == 1


def test_changed_content_creates_new_generation(svc):
    s, z2m, snap = svc
    first = s.take("startup")
    (z2m / "configuration.yaml").write_text("network_key: v2\n")
    second = s.take("devices_changed")

    assert second is not None and second["gen"] != first["gen"]
    assert [g.name for g in _gens(snap)] == [first["gen"], second["gen"]]


def test_rotation_keeps_latest_and_clears_stale_tmp(svc):
    s, z2m, snap = svc
    stale = snap / "gen-0000000000000.tmp"
    stale.mkdir(parents=True)

    taken = []
    for i in range(zigbee_snapshot.KEEP + 2):
        (z2m / "configuration.yaml").write_text(f"network_key: v{i}\n")
        taken.append(s.take("devices_changed")["gen"])

    assert [g.name for g in _gens(snap)] == taken[-zigbee_snapshot.KEEP:]
    assert not stale.exists()


def test_take_failure_is_recorded_not_raised(svc):
    s, z2m, _snap = svc
    (z2m / "configuration.yaml").unlink()
    (z2m / "database.db").unlink()

    assert s.take("devices_changed") is None
    assert "沒有可快照的檔案" in s.status()["last_error"]


# ── disabled mode ───────────────────────────────────────────────────────


def test_unconfigured_service_is_noop():
    s = SnapshotService()
    assert s.enabled is False
    assert s.take("startup") is None
    s.notify_bridge_devices()  # must not need a running loop
    assert s.status() == {
        "enabled": False, "last_snapshot": None, "generations": 0,
        "last_error": None, "last_restore": None,
    }


def test_configure_without_env_is_silent_dev_mode():
    s = SnapshotService()
    s.configure("", "")
    assert s.enabled is False
    assert s.status()["last_error"] is None


def test_configure_with_missing_dir_reports_misconfiguration():
    """設了卻用不起來 ≠ 本機 dev：還原端照樣每次開機蓋回，這種停用必須報異常。"""
    with tempfile.TemporaryDirectory() as d:
        s = SnapshotService()
        s.configure(str(Path(d) / "nope"), str(Path(d) / "snapshots"))
        assert s.enabled is False
        assert "Z2M_DATA_DIR" in s.status()["last_error"]


def test_configure_without_snapshot_dir_reports_misconfiguration(svc):
    _s, z2m, _snap = svc
    s = SnapshotService()
    s.configure(str(z2m), "")
    assert s.enabled is False
    assert "Z2M_SNAPSHOT_DIR" in s.status()["last_error"]


def test_configure_reports_unusable_snapshot_dir_but_keeps_restore_visible(svc):
    s, z2m, _snap = svc
    (z2m / "restore-log.jsonl").write_text('{"ts":9,"gen":"gen-9","ok":1}\n')
    blocker = z2m / "not-a-dir"      # 檔案擋著 → mkdir 失敗
    blocker.write_text("x")

    s.configure(str(z2m), str(blocker / "snapshots"))
    st = s.status()
    assert s.enabled is False
    assert "快照目錄建不起來" in st["last_error"]
    assert st["last_restore"] == {"ts": 9, "gen": "gen-9", "ok": 1}


# ── debounce ────────────────────────────────────────────────────────────


def test_notify_bridge_devices_batches_into_one_generation(svc, monkeypatch):
    s, _z2m, snap = svc
    monkeypatch.setattr(zigbee_snapshot, "DEBOUNCE_S", 0.02)

    async def scenario():
        s.notify_bridge_devices()
        s.notify_bridge_devices()
        s.notify_bridge_devices()
        await asyncio.sleep(0.2)

    asyncio.run(scenario())

    gens = _gens(snap)
    assert len(gens) == 1
    assert json.loads((gens[0] / "meta.json").read_text())["reason"] == "startup"


def test_second_batch_is_devices_changed(svc, monkeypatch):
    s, z2m, snap = svc
    monkeypatch.setattr(zigbee_snapshot, "DEBOUNCE_S", 0.02)

    async def scenario():
        s.notify_bridge_devices()
        await asyncio.sleep(0.2)
        (z2m / "configuration.yaml").write_text("network_key: v2\n")
        s.notify_bridge_devices()
        await asyncio.sleep(0.2)

    asyncio.run(scenario())

    reasons = [json.loads((g / "meta.json").read_text())["reason"] for g in _gens(snap)]
    assert reasons == ["startup", "devices_changed"]


# ── status ──────────────────────────────────────────────────────────────


def test_status_enabled_contract(svc):
    s, _z2m, _snap = svc
    empty = s.status()
    assert set(empty) == STATUS_KEYS
    assert empty == {"enabled": True, "last_snapshot": None, "generations": 0,
                     "last_error": None, "last_restore": None}

    taken = s.take("startup")
    st = s.status()
    assert st["last_snapshot"] == {"ts": taken["ts"], "reason": "startup",
                                   "gen": taken["gen"]}
    assert isinstance(st["last_snapshot"]["ts"], int)
    assert st["generations"] == 1


def test_status_reads_latest_generation_from_disk_after_restart(svc):
    s, z2m, snap = svc
    taken = s.take("startup")

    fresh = SnapshotService()
    fresh.configure(str(z2m), str(snap))
    st = fresh.status()
    assert st["last_snapshot"]["gen"] == taken["gen"]
    assert st["last_snapshot"]["reason"] == "startup"
    assert st["generations"] == 1


def test_status_last_restore_is_last_log_line(svc):
    s, z2m, _snap = svc
    (z2m / "restore-log.jsonl").write_text(
        '{"ts":1,"gen":"gen-1","ok":1,"files":["configuration.yaml"]}\n'
        '{"ts":2,"gen":"gen-2","ok":0,"expected":["configuration.yaml","database.db"],'
        '"files":["configuration.yaml"]}\n'
        "\n"
    )
    assert s.status()["last_restore"] == {
        "ts": 2, "gen": "gen-2", "ok": 0,
        "expected": ["configuration.yaml", "database.db"],
        "files": ["configuration.yaml"],
    }


def test_status_last_restore_tolerates_broken_log(svc):
    s, z2m, _snap = svc
    (z2m / "restore-log.jsonl").write_text('{"ts":1,"gen":"gen-1"}\n{"ts":2,')
    assert s.status()["last_restore"] is None


# ── z2m-restore.sh ──────────────────────────────────────────────────────

RESTORE_SH = Path(__file__).resolve().parents[2] / "deploy" / "z2m-restore.sh"


def _run_restore(tmp: Path, files: dict[str, str], before=None) -> dict:
    """在 tmp 下擺一代快照、跑還原腳本，回傳它寫的紀錄行。"""
    gen = tmp / "snapshots" / "gen-1700000000005"
    gen.mkdir(parents=True)
    for name, content in files.items():
        (gen / name).write_text(content)
    (gen / "meta.json").write_text("{}")
    data = tmp / "data"
    data.mkdir()
    (data / "configuration.yaml").write_text("network_key: ZEROED\n")
    if before:
        before()

    proc = subprocess.run(
        ["sh", str(RESTORE_SH)], capture_output=True, text=True,
        env={**os.environ, "SNAP_DIR": str(tmp / "snapshots"), "DATA_DIR": str(data)},
    )
    assert proc.returncode == 0, proc.stderr      # 還原絕不擋 z2m 啟動
    return json.loads((data / "restore-log.jsonl").read_text().splitlines()[-1])


def test_restore_records_ok_when_every_file_lands():
    """這代本來就沒有 coordinator_backup.json 也算完整——expected 是照這代算的。"""
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        line = _run_restore(tmp, {"configuration.yaml": "network_key: GOOD\n",
                                  "database.db": "db-good\n"})
        assert line["ok"] == 1
        assert line["expected"] == line["files"] == ["configuration.yaml", "database.db"]
        assert (tmp / "data" / "configuration.yaml").read_text() == "network_key: GOOD\n"


@pytest.mark.skipif(os.geteuid() == 0, reason="root 讀得到 chmod 000 的檔案")
def test_restore_records_shortfall_instead_of_looking_healthy():
    """有檔案沒蓋回去 → ok=0；少了它，壞掉那次開機長得跟健康的一模一樣。"""
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        db = tmp / "snapshots" / "gen-1700000000005" / "database.db"

        def unreadable_db():
            db.chmod(0o000)      # 模擬 SD 讀取錯誤：cp 讀不到這一份

        line = _run_restore(tmp, {"configuration.yaml": "network_key: GOOD\n",
                                  "database.db": "db-good\n"}, before=unreadable_db)
        db.chmod(0o644)          # 讓 TemporaryDirectory 收得掉

        assert line["ok"] == 0
        assert line["expected"] == ["configuration.yaml", "database.db"]
        assert line["files"] == ["configuration.yaml"]


# ── endpoint ────────────────────────────────────────────────────────────


def test_snapshot_status_endpoint_enabled(svc, monkeypatch):
    s, _z2m, _snap = svc
    taken = s.take("startup")
    monkeypatch.setattr(buttons_router, "snapshot_service", s)

    body = TestClient(app).get("/api/zigbee/snapshot_status").json()
    assert set(body) == STATUS_KEYS
    assert body["enabled"] is True
    assert body["generations"] == 1
    assert body["last_snapshot"]["gen"] == taken["gen"]


def test_snapshot_status_endpoint_disabled(monkeypatch):
    monkeypatch.setattr(buttons_router, "snapshot_service", SnapshotService())
    body = TestClient(app).get("/api/zigbee/snapshot_status").json()
    assert body == {"enabled": False, "last_snapshot": None, "generations": 0,
                    "last_error": None, "last_restore": None}
