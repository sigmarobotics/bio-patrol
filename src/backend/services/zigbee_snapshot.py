"""z2m 設定快照（斷電韌性）。

現場 Pi 天天被硬斷電，而 zigbee2mqtt 寫 configuration.yaml 用的是非原子的
fs.writeFileSync——斷在寫入中就清零，等於 Zigbee 網路重生（network_key 換了、
裝置清單沒了），現場每顆按鈕都要重配。database.db 雖然已是原子寫，但讀取端遇到
壞行是靜默忽略，壞掉會變成一個沒人察覺的空庫。

對策：裝置清單一變動就把 z2m 的三個檔存成一「代」快照（gen-<epochms>/），
容器下次啟動時由 deploy/z2m-restore.sh 無條件蓋回最新完整代。正常開機蓋回的內容
與現況相同，壞掉的那次開機才救得回來。

代的完整性靠 rename 的原子性：先寫 gen-*.tmp/，meta.json 落地後才 rename 成
gen-*——所以「目錄裡有 meta.json」＝這一代寫完了，還原端據此挑選。

env 未設或路徑不存在（本機 dev 沒掛 z2m 資料夾）→ disabled，所有操作 no-op。
"""

import asyncio
import hashlib
import json
import logging
import os
import shutil
import time
from pathlib import Path

logger = logging.getLogger("services.zigbee_snapshot")

# 快照的檔案（存在者才收）：網路身分＋裝置清單＋協調器備份。
FILES = ("configuration.yaml", "database.db", "coordinator_backup.json")
KEEP = 5            # 保留幾代
DEBOUNCE_S = 5.0    # bridge/devices 常一次來好幾則（配對／改名），等它安靜再存


class SnapshotService:
    def __init__(self):
        self._z2m_dir: Path | None = None
        self._snap_dir: Path | None = None
        self._last: dict | None = None
        self._last_error: str | None = None
        self._task: asyncio.Task | None = None
        self._seen_bridge = False
        self._pending = "startup"

    @property
    def enabled(self) -> bool:
        return self._z2m_dir is not None

    def configure(self, z2m_dir: str = "", snap_dir: str = "") -> None:
        """main.py lifespan 注入路徑；任一項缺失就停用（本機 dev 不炸）。"""
        self._z2m_dir = self._snap_dir = None
        if not z2m_dir or not snap_dir or not Path(z2m_dir).is_dir():
            logger.info("z2m 快照未啟用（Z2M_DATA_DIR=%r）", z2m_dir)
            return
        try:
            Path(snap_dir).mkdir(parents=True, exist_ok=True)
        except OSError as e:
            logger.warning("z2m 快照目錄建立失敗：%s", e)
            return
        self._z2m_dir, self._snap_dir = Path(z2m_dir), Path(snap_dir)
        logger.info("z2m 快照已啟用：%s → %s", z2m_dir, snap_dir)

    # ── 觸發 ────────────────────────────────────────────────────────────

    def notify_bridge_devices(self) -> None:
        """收到 bridge/devices（裝置清單變動）→ 去抖後存一代。"""
        if not self.enabled:
            return
        if self._task and not self._task.done():
            self._task.cancel()      # 併進同一批，reason 沿用批中第一則訊息的
        else:
            self._pending = "devices_changed" if self._seen_bridge else "startup"
        self._seen_bridge = True
        self._task = asyncio.create_task(self._debounced(self._pending))

    async def _debounced(self, reason: str) -> None:
        try:
            await asyncio.sleep(DEBOUNCE_S)
        except asyncio.CancelledError:
            return
        await asyncio.to_thread(self.take, reason)

    # ── 存一代 ──────────────────────────────────────────────────────────

    def take(self, reason: str) -> dict | None:
        """存一代快照；內容與最新一代相同則不存。失敗只記錄不拋（快照不能擋主線）。"""
        if not self.enabled:
            return None
        try:
            return self._take(reason)
        except Exception as e:
            self._last_error = f"{reason}：{e}"
            logger.warning("z2m 快照失敗（%s）：%s", reason, e)
            return None

    def _take(self, reason: str) -> dict | None:
        blobs = {n: (self._z2m_dir / n).read_bytes()
                 for n in FILES if (self._z2m_dir / n).is_file()}
        if not blobs:
            raise FileNotFoundError(f"{self._z2m_dir} 沒有可快照的檔案")
        hashes = {n: hashlib.sha256(b).hexdigest() for n, b in blobs.items()}

        latest = self._latest()
        if latest and latest[1].get("hashes") == hashes:
            self._last_error = None
            return None

        # 同一毫秒內連存兩次會撞名，而 rename 到既有目錄是會失敗的——往後挪到空位
        # （名稱的字典序＝時間序，這條性質要保住：挑最新一代、輪替都靠它）。
        ts_ms = int(time.time() * 1000)
        while (self._snap_dir / f"gen-{ts_ms}").exists():
            ts_ms += 1
        gen = f"gen-{ts_ms}"
        tmp = self._snap_dir / f"{gen}.tmp"
        shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir(parents=True)
        for name, blob in blobs.items():
            _write(tmp / name, blob)
        meta = {"ts": int(time.time()), "reason": reason, "hashes": hashes}
        _write(tmp / "meta.json",
               json.dumps(meta, ensure_ascii=False).encode())
        _fsync_dir(tmp)
        os.rename(tmp, self._snap_dir / gen)
        _fsync_dir(self._snap_dir)

        self._rotate()
        self._last_error = None
        self._last = {"ts": meta["ts"], "reason": reason, "gen": gen}
        logger.info("z2m 快照 %s（%s，%d 檔）", gen, reason, len(blobs))
        return self._last

    def _rotate(self) -> None:
        for tmp in self._snap_dir.glob("*.tmp"):
            shutil.rmtree(tmp, ignore_errors=True)
        for old in self._gens()[:-KEEP]:
            shutil.rmtree(old, ignore_errors=True)

    def _gens(self) -> list[Path]:
        """完成的代，由舊到新。gen-<epochms> 的字典序＝時間序。"""
        return sorted((p for p in self._snap_dir.glob("gen-*")
                       if p.is_dir() and not p.name.endswith(".tmp")
                       and (p / "meta.json").is_file()),
                      key=lambda p: p.name)

    def _latest(self) -> tuple[Path, dict] | None:
        for path in reversed(self._gens()):
            try:
                return path, json.loads((path / "meta.json").read_text())
            except (OSError, json.JSONDecodeError):
                continue
        return None

    # ── 狀態（/api/zigbee/snapshot_status）────────────────────────────

    def status(self) -> dict:
        """ts 一律 epoch 秒，時區換算交給前端。"""
        if not self.enabled:
            return {"enabled": False, "last_snapshot": None, "generations": 0,
                    "last_error": self._last_error, "last_restore": None}
        last = self._last
        if last is None:
            latest = self._latest()
            if latest:
                last = {"ts": latest[1].get("ts"),
                        "reason": latest[1].get("reason"), "gen": latest[0].name}
        return {"enabled": True, "last_snapshot": last,
                "generations": len(self._gens()),
                "last_error": self._last_error,
                "last_restore": self._last_restore()}

    def _last_restore(self) -> dict | None:
        """z2m 容器每次啟動由 z2m-restore.sh append 一行；取最後一行。"""
        try:
            lines = [ln for ln in
                     (self._z2m_dir / "restore-log.jsonl").read_text().splitlines()
                     if ln.strip()]
            return json.loads(lines[-1]) if lines else None
        except (OSError, ValueError):
            return None


def _write(path: Path, blob: bytes) -> None:
    with open(path, "wb") as f:
        f.write(blob)
        f.flush()
        os.fsync(f.fileno())


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


snapshot_service = SnapshotService()
