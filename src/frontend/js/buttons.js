// Settings tab — Zigbee Buttons panel.
// One row per registered action; Pair / Cancel / Unpair / Test per row.

(function () {
  const POLL_INTERVAL_MS = 3000;
  const PAIR_COUNTDOWN_MS = 1000;

  let pollTimer = null;
  let countdownTimer = null;
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

      // If server reports a different armed_action than us, sync.
      const armed = data.pair_status?.armed_action;
      if (armed && (!pairingState || pairingState.action_key !== armed)) {
        pairingState = {
          action_key: armed,
          deadline_ts: Date.now() + 120 * 1000,
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
    } catch (e) {
      // ignore
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
      // Re-render only the pairing row to update the countdown.
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
  }

  // Public init — called from script.js loadSettings().
  window.loadButtons = function () {
    stopTimers();
    refresh();
    pollTimer = setInterval(refresh, POLL_INTERVAL_MS);
    startCountdown();
  };

  // Cleanup if user leaves settings tab.
  window.unloadButtons = stopTimers;
})();
