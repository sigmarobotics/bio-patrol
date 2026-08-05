// Settings tab — Zigbee Buttons panel.
(function () {
  const POLL_INTERVAL_MS = 3000;
  const PAIR_COUNTDOWN_MS = 1000;
  const SNAPSHOT_POLL_MS = 10000;
  const TOAST_MS = 6000;
  const SEEN_RESTORE_KEY = "z2m_snap_seen_restore_ts";
  const SEEN_ERROR_KEY = "z2m_snap_seen_error";

  let pollTimer = null;
  let countdownTimer = null;
  let snapshotTimer = null;
  let pairingState = null; // {action_key, deadline_ts}

  async function api(path, opts = {}) {
    const res = await fetch(path, {
      headers: { "Content-Type": "application/json" },
      ...opts,
    });
    if (!res.ok) {
      const text = await res.text();
      throw new Error(`${res.status}: ${text}`);
    }
    return res.json();
  }

  function fmtRelative(iso) {
    if (!iso) return "—";
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return "—";
    const seconds = Math.round((Date.now() - d.getTime()) / 1000);
    if (seconds < 60) return `${seconds}s ago`;
    const m = Math.round(seconds / 60);
    if (m < 60) return `${m}m ago`;
    const h = Math.round(m / 60);
    if (h < 48) return `${h}h ago`;
    return d.toLocaleString();
  }

  // Snapshot status carries epoch seconds (see /api/zigbee/snapshot_status).
  function fmtEpoch(ts) {
    if (!ts) return "—";
    return fmtRelative(new Date(ts * 1000).toISOString());
  }

  function toast(message, isError = false) {
    const el = document.createElement("div");
    el.className = `z2m-toast${isError ? " z2m-toast--error" : ""}`;
    el.textContent = message;
    document.body.appendChild(el);
    setTimeout(() => el.remove(), TOAST_MS);
  }

  // ok === 0 → z2m-restore.sh 有檔案沒蓋回去（舊格式沒有 ok，視為正常）。
  function restoreFailed(restore) {
    return !!restore && restore.ok === 0;
  }

  function maybeToastSnapshot(data) {
    const restore = data.last_restore;
    const restoreTs = restore?.ts;
    if (restoreTs && String(restoreTs) !== localStorage.getItem(SEEN_RESTORE_KEY)) {
      localStorage.setItem(SEEN_RESTORE_KEY, String(restoreTs));
      const gen = restore.gen || "—";
      toast(
        restoreFailed(restore)
          ? `Zigbee 設定開機還原不完整（${gen}）：z2m 可能是用壞掉的設定啟動的`
          : `Zigbee 設定已於開機時還原（${gen}）`,
        restoreFailed(restore),
      );
    }
    const err = data.last_error || "";
    if (err !== (localStorage.getItem(SEEN_ERROR_KEY) || "")) {
      localStorage.setItem(SEEN_ERROR_KEY, err);
      if (err) toast(`Zigbee 設定快照異常：${err}`, true);
    }
  }

  async function refreshSnapshot() {
    const card = document.getElementById("z2m-snap-card");
    if (!card) return;
    const pill = document.getElementById("z2m-snap-pill");
    const lastEl = document.getElementById("z2m-snap-last");
    const gensEl = document.getElementById("z2m-snap-gens");
    const restoreEl = document.getElementById("z2m-snap-restore");
    const errEl = document.getElementById("z2m-snap-error");

    let data;
    try {
      data = await api("/api/zigbee/snapshot_status");
    } catch (e) {
      pill.textContent = "讀取失敗";
      pill.className = "bb-pill bb-pill--bad";
      return;
    }

    // 停用分兩種：env 沒設＝本機開發（中性）；設了卻用不起來＝正式機誤設，快照
    // 停了但還原沒停，每次開機都會把系統拖回停掉那一刻——必須報異常。
    const restore = data.last_restore;
    const badRestore = restoreFailed(restore);
    const errors = [];
    if (data.last_error) {
      errors.push(
        data.enabled
          ? `快照失敗：${data.last_error}`
          : `快照已停用（沒在存新的，但開機還原照樣會蓋）：${data.last_error}`,
      );
    }
    if (badRestore) {
      const got = (restore.files || []).length;
      const want = (restore.expected || []).length;
      errors.push(
        `上次開機還原不完整（${got}/${want} 檔）：z2m 可能是用壞掉的設定啟動的，` +
          "請確認按鈕是否還在配對狀態",
      );
    }

    const snap = data.last_snapshot;
    if (!data.enabled) {
      lastEl.textContent = data.last_error ? "已停用（設定有問題）" : "未啟用（本機開發）";
      gensEl.textContent = "—";
    } else {
      lastEl.textContent = snap
        ? `${fmtEpoch(snap.ts)} · ${snap.reason || "—"}`
        : "尚無快照";
      gensEl.textContent = `${data.generations} 代`;
    }
    restoreEl.textContent = restore
      ? `${fmtEpoch(restore.ts)} · ${restore.gen || "—"}${badRestore ? " · 不完整" : ""}`
      : "無紀錄";

    if (errors.length) {
      card.classList.add("z2m-snap--error");
      errEl.hidden = false;
      errEl.textContent = errors.join("；");
      pill.textContent = "異常";
      pill.className = "bb-pill bb-pill--bad";
    } else {
      card.classList.remove("z2m-snap--error");
      errEl.hidden = true;
      pill.textContent = data.enabled ? "運作中" : "未啟用";
      pill.className = data.enabled ? "bb-pill bb-pill--ok" : "bb-pill";
    }

    maybeToastSnapshot(data);
  }

  function renderRow(row) {
    const isArmed = pairingState && pairingState.action_key === row.key;
    const isBound = !!row.ieee_addr;

    let statusHtml = "";
    if (isArmed) {
      const remaining = Math.max(
        0,
        Math.round((pairingState.deadline_ts - Date.now()) / 1000),
      );
      statusHtml = `<span class="bb-status bb-status--pairing">配對中… ${remaining}s</span>`;
    } else if (isBound) {
      const battery =
        row.battery == null ? "" : ` · 電量 ${row.battery}%`;
      const lastSeen = fmtRelative(row.last_seen);
      statusHtml = `<span class="bb-status bb-status--bound">
        <code>${row.ieee_addr}</code>${battery} · 最後上線 ${lastSeen}
      </span>`;
    } else {
      statusHtml = `<span class="bb-status bb-status--unpaired">尚未配對</span>`;
    }

    let actionsHtml = "";
    if (isArmed) {
      actionsHtml = `<button class="btn-secondary" data-bb-cancel="${row.key}">取消配對</button>`;
    } else if (isBound) {
      actionsHtml = `
        <button class="btn-secondary" data-bb-test="${row.key}">測試</button>
        <button class="btn-danger" data-bb-unpair="${row.key}">解除配對</button>
      `;
    } else {
      actionsHtml = `
        <button class="btn-success" data-bb-pair="${row.key}">配對</button>
        <button class="btn-secondary" data-bb-test="${row.key}">測試</button>
      `;
    }

    const fired =
      row.fire_count > 0
        ? `<span class="bb-fired">已觸發 ${row.fire_count}次 · 上次 ${fmtRelative(row.last_fired_at)}</span>`
        : "";

    return `
      <div class="bb-row" data-bb-action="${row.key}">
        <div class="bb-info">
          <div class="bb-label">${row.label}</div>
          <div class="bb-meta">${statusHtml} ${fired}</div>
        </div>
        <div class="bb-actions">${actionsHtml}</div>
      </div>
    `;
  }

  function bindRowHandlers(container) {
    container.querySelectorAll("[data-bb-pair]").forEach((b) =>
      b.addEventListener("click", () => onPair(b.dataset.bbPair)),
    );
    container.querySelectorAll("[data-bb-cancel]").forEach((b) =>
      b.addEventListener("click", () => onCancel(b.dataset.bbCancel)),
    );
    container.querySelectorAll("[data-bb-unpair]").forEach((b) =>
      b.addEventListener("click", () => onUnpair(b.dataset.bbUnpair)),
    );
    container.querySelectorAll("[data-bb-test]").forEach((b) =>
      b.addEventListener("click", () => onTest(b.dataset.bbTest)),
    );
  }

  async function refresh() {
    const root = document.getElementById("bb-list");
    if (!root) return;
    try {
      const data = await api("/api/button-bindings");
      const mqttPill = document.getElementById("bb-mqtt-pill");
      if (mqttPill) {
        const ok = data.pair_status?.mqtt_connected;
        mqttPill.textContent = ok ? "MQTT 已連線" : "MQTT 未連線";
        mqttPill.className = `bb-pill ${ok ? "bb-pill--ok" : "bb-pill--bad"}`;
      }

      const armed = data.pair_status?.armed_action;
      const remaining = data.pair_status?.armed_remaining_s;
      if (armed && (!pairingState || pairingState.action_key !== armed)) {
        pairingState = {
          action_key: armed,
          deadline_ts: Date.now() + (remaining ?? 120) * 1000,
        };
      } else if (!armed && pairingState) {
        pairingState = null;
      }

      root.innerHTML = data.actions.map(renderRow).join("");
      bindRowHandlers(root);
    } catch (e) {
      root.innerHTML = `<div class="bb-error">載入失敗：${e.message}</div>`;
    }
  }

  async function onPair(actionKey) {
    try {
      const result = await api(
        `/api/button-bindings/${encodeURIComponent(actionKey)}/pair`,
        { method: "POST" },
      );
      pairingState = {
        action_key: actionKey,
        deadline_ts: Date.now() + (result.timeout || 120) * 1000,
      };
      await refresh();
    } catch (e) {
      alert("無法開始配對：" + e.message);
    }
  }

  async function onCancel(actionKey) {
    try {
      await api(
        `/api/button-bindings/${encodeURIComponent(actionKey)}/pair/cancel`,
        { method: "POST" },
      );
    } catch (_) {
      // best-effort cancel — refresh below will re-sync from server state
    } finally {
      pairingState = null;
      await refresh();
    }
  }

  async function onUnpair(actionKey) {
    if (!confirm("確定要解除這顆按鈕的配對？")) return;
    try {
      await api(
        `/api/button-bindings/${encodeURIComponent(actionKey)}?forget_device=true`,
        { method: "DELETE" },
      );
      await refresh();
    } catch (e) {
      alert("解除配對失敗：" + e.message);
    }
  }

  async function onTest(actionKey) {
    try {
      const result = await api(
        `/api/button-bindings/${encodeURIComponent(actionKey)}/test`,
        { method: "POST", body: JSON.stringify({}) },
      );
      const ok = result?.ok ?? result?.status === "ok";
      const detail = result?.error || result?.message || JSON.stringify(result);
      alert(ok ? `測試成功：${detail}` : `測試失敗：${detail}`);
      await refresh();
    } catch (e) {
      alert("測試失敗：" + e.message);
    }
  }

  function startCountdown() {
    if (countdownTimer) return;
    countdownTimer = setInterval(() => {
      if (!pairingState) return;
      if (Date.now() > pairingState.deadline_ts) {
        pairingState = null;
        refresh();
        return;
      }
      const row = document.querySelector(
        `[data-bb-action="${pairingState.action_key}"] .bb-status--pairing`,
      );
      if (row) {
        const remaining = Math.max(
          0,
          Math.round((pairingState.deadline_ts - Date.now()) / 1000),
        );
        row.textContent = `配對中… ${remaining}s`;
      }
    }, PAIR_COUNTDOWN_MS);
  }

  function stopTimers() {
    if (pollTimer) {
      clearInterval(pollTimer);
      pollTimer = null;
    }
    if (countdownTimer) {
      clearInterval(countdownTimer);
      countdownTimer = null;
    }
    if (snapshotTimer) {
      clearInterval(snapshotTimer);
      snapshotTimer = null;
    }
  }

  window.loadButtons = function () {
    stopTimers();
    refresh();
    pollTimer = setInterval(refresh, POLL_INTERVAL_MS);
    startCountdown();
    refreshSnapshot();
    snapshotTimer = setInterval(refreshSnapshot, SNAPSHOT_POLL_MS);
  };

  window.unloadButtons = stopTimers;
})();
