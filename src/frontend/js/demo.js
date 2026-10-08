// IT-21 /demo panel — polls /api/demo/live every second and renders the seat
// cards, robot map, progress and the Telegram notification preview. Every
// value shown here is synthetic and comes from demo_data.db.

// mapView.js reads these bare globals (shared with the dashboard's script.js).
var robotData = { pose: null };
var shelfDropPose = null;

(function () {
  const POLL_MS = 1000;
  const STATE_TEXT = {
    pending: '待量測',
    measuring: '量測中',
    normal: '正常',
    abnormal: '心跳呼吸異常',
    absent: '偵測不到人',
    skipped: '未量測',
  };
  const ROBOT_TEXT = {
    connected: '已連線',
    disconnected: '已斷線',
    unregistered: '未連線',
    unknown: '未知',
  };

  let live = null;
  let liveAt = 0;          // performance.now() of the last /live response
  let lastNotifyKey = '';

  const $ = (id) => document.getElementById(id);

  function esc(s) {
    return String(s ?? '').replace(/[&<>"']/g, (c) => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    }[c]));
  }

  function hhmm(iso) {
    if (!iso) return '';
    const m = /T(\d{2}:\d{2})/.exec(iso);
    return m ? m[1] : '';
  }

  // ── Seats ────────────────────────────────────────────────────────────────
  function remainingNow(seat) {
    if (seat.remaining_seconds == null) return null;
    const drift = (performance.now() - liveAt) / 1000;
    return Math.max(0, seat.remaining_seconds - drift);
  }

  function seatBody(seat) {
    if (seat.state === 'measuring') {
      const rem = remainingNow(seat);
      const total = Number(seat.seconds) || 1;
      const pct = rem == null ? 0 : Math.min(100, (1 - rem / total) * 100);
      return `<div class="seat-card__state">量測中 <span class="seat-card__secs">${rem == null ? '' : Math.ceil(rem) + ' 秒'}</span></div>
        <div class="seat-card__countdown"><div style="width:${pct}%"></div></div>`;
    }
    let html = `<div class="seat-card__state">${STATE_TEXT[seat.state] || seat.state}</div>`;
    if (seat.state === 'normal' || seat.state === 'abnormal') {
      html += `<div class="seat-card__values">心跳 <b>${esc(seat.bpm)}</b>・呼吸 <b>${esc(seat.rpm)}</b></div>`;
    }
    return html;
  }

  function renderSeats() {
    const box = $('demo-seats');
    const seats = live?.seats || [];
    $('demo-empty').hidden = seats.length > 0;
    box.innerHTML = seats.map((s) => `
      <div class="seat-card seat--${esc(s.state)}" data-bed-key="${esc(s.bed_key)}" data-state="${esc(s.state)}">
        <div class="seat-card__name">${esc(s.bed_key)}</div>
        ${seatBody(s)}
      </div>`).join('');
  }

  // Countdown ticks between polls without waiting for the next response.
  function tickCountdowns() {
    if (!live) return;
    for (const seat of live.seats || []) {
      if (seat.state !== 'measuring') continue;
      const card = document.querySelector(`.seat-card[data-bed-key="${CSS.escape(seat.bed_key)}"]`);
      if (!card) continue;
      const rem = remainingNow(seat);
      const secs = card.querySelector('.seat-card__secs');
      if (secs && rem != null) secs.textContent = `${Math.ceil(rem)} 秒`;
      const bar = card.querySelector('.seat-card__countdown > div');
      if (bar && rem != null) {
        bar.style.width = `${Math.min(100, (1 - rem / (Number(seat.seconds) || 1)) * 100)}%`;
      }
    }
  }

  // ── Banners, header, progress ────────────────────────────────────────────
  function renderStatus() {
    const robot = live?.robot || {};
    $('demo-disconnect-banner').hidden = !!robot.connected;
    $('demo-robot-state').textContent = ROBOT_TEXT[robot.state] || robot.state || '--';

    const abnormal = (live?.seats || []).filter((s) => s.state === 'abnormal');
    const alert = $('demo-alert-banner');
    alert.hidden = abnormal.length === 0;
    alert.textContent = abnormal.length
      ? `⚠️ 心跳呼吸異常：${abnormal.map((s) => `座位 ${s.bed_key}`).join('、')}`
      : '';

    const running = !!live?.running;
    const hasSeats = (live?.seats || []).length > 0;
    $('demo-idle-hint').hidden = running || !hasSeats;
    $('demo-seats-title').textContent = running || !hasSeats ? '座位量測' : '上一次 Demo 結果';

    const { done = 0, total = 0 } = live?.progress || {};
    $('demo-progress-text').textContent = `${done} / ${total}`;
    $('demo-progress-bar').style.width = total ? `${(done / total) * 100}%` : '0';

    robotData.pose = robot.pose || null;
  }

  // ── Notification preview ─────────────────────────────────────────────────
  async function refreshNotifications() {
    const taskId = live?.task_id;
    if (!taskId) return;
    try {
      const res = await fetch(`/api/demo/notifications?task_id=${encodeURIComponent(taskId)}`);
      const data = await res.json();
      const items = data.items || [];
      const key = `${taskId}:${items.length}`;
      if (key === lastNotifyKey) return;
      lastNotifyKey = key;
      const chat = $('demo-chat');
      chat.innerHTML = items.length
        ? items.map((n) => `
          <div class="chat-msg chat-msg--${esc(n.severity)}" data-source="${esc(n.source)}">
            <div class="chat-avatar">🤖</div>
            <div class="chat-bubble"><span class="chat-bubble__title">${esc(n.title)}</span>${esc(n.body)}<span class="chat-bubble__time">${hhmm(n.timestamp)}</span></div>
          </div>`).join('')
        : '<p class="demo-chat-empty">尚無通報</p>';
      chat.scrollTop = chat.scrollHeight;
    } catch (e) {
      // Keep the last preview on screen; the next poll retries.
    }
  }

  // ── Polling ──────────────────────────────────────────────────────────────
  async function poll() {
    try {
      const res = await fetch('/api/demo/live');
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      live = await res.json();
      liveAt = performance.now();
      renderSeats();
      renderStatus();
      refreshNotifications();
    } catch (e) {
      // Backend unreachable — say so instead of looking frozen.
      $('demo-disconnect-banner').hidden = false;
    }
  }

  // ── Seat drawer: synthetic trend + average ───────────────────────────────
  function sparkline(points, key, color, band) {
    const vals = points.map((p) => p[key]);
    if (!vals.length) return '<p class="demo-drawer-note">無資料</p>';
    const W = 400, H = 120, P = 8;
    const lo = Math.min(...vals, band[0]) - 2;
    const hi = Math.max(...vals, band[1]) + 2;
    const x = (i) => P + (vals.length === 1 ? (W - 2 * P) / 2 : (i * (W - 2 * P)) / (vals.length - 1));
    const y = (v) => H - P - ((v - lo) * (H - 2 * P)) / (hi - lo || 1);
    const line = vals.map((v, i) => `${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(' ');
    const dots = vals.map((v, i) => {
      const out = v < band[0] || v > band[1];
      return `<circle cx="${x(i).toFixed(1)}" cy="${y(v).toFixed(1)}" r="${out ? 5 : 2.5}" fill="${out ? 'var(--coral)' : color}"/>`;
    }).join('');
    return `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none">
      <rect x="0" y="${y(band[1]).toFixed(1)}" width="${W}" height="${(y(band[0]) - y(band[1])).toFixed(1)}" fill="rgba(40,167,69,0.08)"/>
      <polyline points="${line}" fill="none" stroke="${color}" stroke-width="2"/>${dots}</svg>`;
  }

  async function openDrawer(bedKey) {
    $('demo-drawer-title').textContent = `座位 ${bedKey}`;
    const body = $('demo-drawer-body');
    body.innerHTML = '<p class="demo-drawer-note">載入中…</p>';
    $('demo-drawer').hidden = false;
    try {
      const res = await fetch(`/api/demo/history/${encodeURIComponent(bedKey)}`);
      const data = await res.json();
      const valid = (data.points || []).filter((p) => p.is_valid);
      const t = live?.thresholds || { hr_low: 50, hr_high: 120, rr_low: 10, rr_high: 30 };
      body.innerHTML = `
        <div class="demo-avg">
          <div><div class="label">平均心跳</div><div class="value" id="demo-avg-bpm">${data.average?.bpm ?? '--'}</div></div>
          <div><div class="label">平均呼吸</div><div class="value" id="demo-avg-rpm">${data.average?.rpm ?? '--'}</div></div>
        </div>
        <div class="demo-trend"><h4>心跳趨勢（次／分）</h4>${sparkline(valid, 'bpm', 'var(--amber)', [t.hr_low, t.hr_high])}</div>
        <div class="demo-trend"><h4>呼吸趨勢（次／分）</h4>${sparkline(valid, 'rpm', 'var(--teal)', [t.rr_low, t.rr_high])}</div>
        <p class="demo-drawer-note">近 30 天合成資料（${valid.length} 筆有效量測），綠色區間為正常範圍。</p>`;
    } catch (e) {
      body.innerHTML = '<p class="demo-drawer-note">趨勢載入失敗</p>';
    }
  }

  function closeDrawer() { $('demo-drawer').hidden = true; }

  window.addEventListener('DOMContentLoaded', () => {
    $('demo-seats').addEventListener('click', (e) => {
      const card = e.target.closest('.seat-card');
      if (card) openDrawer(card.dataset.bedKey);
    });
    $('demo-drawer-close').addEventListener('click', closeDrawer);
    $('demo-drawer-backdrop').addEventListener('click', closeDrawer);
    document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeDrawer(); });
    if (window.mapView) mapView.init('demo-map-canvas', { interactive: false, fit: true });
    poll();
    setInterval(poll, POLL_MS);
    setInterval(tickCountdowns, 250);
  });
})();
