// Bed-grid + drawer logic for the dashboard.
(function (global) {
  const POLL_INTERVAL_MS = 10000;
  const DEFAULT_STALE_HOURS = 24;
  const BED_STATE = Object.freeze({
    VALID: 'valid',
    STALE: 'stale',
    INVALID: 'invalid',
    UNSCHEDULED: 'unscheduled',
  });

  function classifyBedState(bed, latest, isPatrolEnabled, staleHours, now = Date.now()) {
    if (!isPatrolEnabled) return BED_STATE.UNSCHEDULED;
    if (!latest) return BED_STATE.STALE;
    if (!latest.is_valid) return BED_STATE.INVALID;
    const ts = Date.parse(latest.timestamp);
    if (Number.isNaN(ts)) return BED_STATE.STALE;
    const ageMs = now - ts;
    const thresholdMs = staleHours * 3600 * 1000;
    return ageMs > thresholdMs ? BED_STATE.STALE : BED_STATE.VALID;
  }

  function formatRelativeTime(timestamp, now = Date.now()) {
    if (!timestamp) return '--';
    const ts = Date.parse(timestamp);
    if (Number.isNaN(ts)) return '--';
    const diffSec = Math.max(0, Math.floor((now - ts) / 1000));
    if (diffSec < 60) return '剛剛';
    const min = Math.floor(diffSec / 60);
    if (min < 60) return `${min} 分前`;
    const hr = Math.floor(min / 60);
    if (hr < 24) return `${hr} 小時前`;
    const day = Math.floor(hr / 24);
    return `${day} 天前`;
  }

  function renderBedCard(bed, latest, isPatrolEnabled, staleHours, now = Date.now()) {
    const state = classifyBedState(bed, latest, isPatrolEnabled, staleHours, now);
    const bedKey = bed.bed_key;
    let vit = '— / —';
    let ts = '無資料';
    let extra = '';

    if (state === BED_STATE.UNSCHEDULED) {
      ts = '未排程';
    } else if (state === BED_STATE.INVALID && latest) {
      extra = latest.details || (latest.status != null ? `status=${latest.status}` : '');
      ts = `${formatRelativeTime(latest.timestamp, now)} 失敗`;
    } else if (latest) {
      vit = `${latest.bpm ?? '--'}/${latest.rpm ?? '--'}`;
      ts = formatRelativeTime(latest.timestamp, now);
    }

    return `<div class="bed-card bed-card--${state}" data-bed-key="${bedKey}" onclick="bedGrid.openDrawer('${bedKey}')">
    <span class="bed-card__bk">${bedKey}</span>
    <span class="bed-card__vit">${vit}</span>
    <span class="bed-card__ts">${ts}${extra ? ' · ' + extra : ''}</span>
  </div>`;
  }

  const _collapsedRooms = new Set();  // module-level: persists during session

  function renderBedGrid(bedsConfig, patrolConfig, latestByBedList, staleHours) {
    const beds = bedsConfig?.beds || {};
    const orderLookup = {};
    (patrolConfig?.beds_order || []).forEach(e => { orderLookup[e.bed_key] = e; });
    // Key by bed_name (= bed_key) because two beds can share a location_id
    // (Kachaka destination). Keying by location_id makes both cards show the
    // same record — exactly the dashboard "103-3 should be empty" symptom.
    const latestByBedName = {};
    (latestByBedList || []).forEach(r => { if (r.bed_name) latestByBedName[r.bed_name] = r; });

    // Group beds by room
    const roomGroups = {};
    for (const [bedKey, bed] of Object.entries(beds)) {
      const room = bed.room || bedKey.split('-')[0];
      if (!roomGroups[room]) roomGroups[room] = [];
      roomGroups[room].push({ bed_key: bedKey, ...bed });
    }

    const now = Date.now();
    const sortedRooms = Object.keys(roomGroups).sort((a, b) => parseInt(a) - parseInt(b));
    let html = '';
    for (const room of sortedRooms) {
      const bedsInRoom = roomGroups[room].sort((a, b) => (a.bed || 0) - (b.bed || 0));
      const enabledCount = bedsInRoom.filter(b => orderLookup[b.bed_key]?.enabled).length;
      const isCollapsed = _collapsedRooms.has(room);

      html += `<section class="room-section${isCollapsed ? ' room-section--collapsed' : ''}" data-room="${room}">
      <header class="room-header" onclick="bedGrid.toggleRoom('${room}')">
        <span class="room-header__caret">${isCollapsed ? '▶' : '▼'}</span>
        <span class="room-header__label">ROOM ${room}</span>
        <span class="room-header__count">${enabledCount}/${bedsInRoom.length}</span>
      </header>
      <div class="bed-grid">
        ${bedsInRoom.map(b => renderBedCard(
          b,
          latestByBedName[b.bed_key],
          !!orderLookup[b.bed_key]?.enabled,
          staleHours,
          now
        )).join('')}
      </div>
    </section>`;
    }
    return html;
  }

  function toggleRoom(room) {
    if (_collapsedRooms.has(room)) _collapsedRooms.delete(room);
    else _collapsedRooms.add(room);
    // Re-render (loadBedGrid caches lastSuccessfulData; just call render again)
    if (_lastData) {
      document.getElementById('bed-grid-main').innerHTML =
        renderBedGrid(_lastData.beds, _lastData.patrol, _lastData.latest, _lastData.staleHours);
    }
  }

  // Module-level state for polling + caches.
  const _pollState = { intervalId: null, visibilityHandler: null };
  let _lastData = null;             // cached for re-render on collapse + 5xx tolerance
  let _staleHoursCache = DEFAULT_STALE_HOURS;  // cached at init; only refreshed when settings change
  let _bedsCache = null;            // cached at init
  let _patrolCache = null;          // cached at init

  async function loadBedGrid() {
    try {
      // Only refetch latest-by-bed every cycle (review feedback).
      // Beds + patrol cached from init; refresh on visible settings/patrol save.
      const latestRes = await dataService.getLatestByBed();
      if (latestRes.status !== 'success') {
        console.warn('latest-by-bed not OK:', latestRes);
        return; // keep last data — don't clear
      }
      _lastData = { beds: _bedsCache, patrol: _patrolCache, latest: latestRes.data, staleHours: _staleHoursCache };
      const html = renderBedGrid(_bedsCache, _patrolCache, latestRes.data, _staleHoursCache);
      const main = document.getElementById('bed-grid-main');
      if (main) main.innerHTML = html;
    } catch (e) {
      console.warn('loadBedGrid failed:', e); // keep last data
    }
  }

  async function _refreshConfig() {
    // Called by init() and exposed via refreshConfig() for save-event hooks
    const [bedsConfig, patrolConfig, settings] = await Promise.all([
      dataService.getBeds(),
      dataService.getPatrol(),
      dataService.getSettings(),
    ]);
    _bedsCache = bedsConfig;
    _patrolCache = patrolConfig;
    _staleHoursCache = settings?.bed_card_stale_hours ?? DEFAULT_STALE_HOURS;
  }

  async function init() {
    if (_pollState.intervalId) return; // already running
    await _refreshConfig();
    await loadBedGrid();
    _pollState.intervalId = setInterval(() => {
      if (!document.hidden) loadBedGrid();
    }, POLL_INTERVAL_MS);
    _pollState.visibilityHandler = () => {
      if (!document.hidden) loadBedGrid();  // resume immediately on visibility-restore
    };
    document.addEventListener('visibilitychange', _pollState.visibilityHandler);
  }

  function refreshConfig() { _refreshConfig().then(() => loadBedGrid()); }

  function teardown() {
    if (_pollState.intervalId) clearInterval(_pollState.intervalId);
    if (_pollState.visibilityHandler) document.removeEventListener('visibilitychange', _pollState.visibilityHandler);
    _pollState.intervalId = null;
    _pollState.visibilityHandler = null;
  }

  // Per-open drawer state: accumulated raw rows (keyed by row id so paged
  // fetches can overlap), the pagination cursor, and the scroll listener.
  const _drawer = { locationId: null, rows: new Map(), oldestId: null, loading: false, done: false, scrollHandler: null };
  const WINDOW_OPTIONS = [10, 20, 30, 50, 100];
  const DEFAULT_WINDOW = 30;
  const PAGE_SIZE = 100;

  function _statsWindow() {
    const saved = parseInt(localStorage.getItem('bedStatsWindow'), 10);
    return WINDOW_OPTIONS.includes(saved) ? saved : DEFAULT_WINDOW;
  }

  // One chart per series, each normalized to its own min/max — BPM (~60-90)
  // and RPM (~12-20) on a shared scale would flatten the RPM trend.
  function _renderSparkline(trend) {
    if (trend.length < 2) return '<p class="bed-stats__empty">尚無有效量測</p>';
    const W = 100, H = 32, PAD = 3;
    const px = i => (i / (trend.length - 1)) * W;
    const chart = (key, label, color) => {
      const values = trend.map(p => p[key]);
      const min = Math.min(...values);
      const max = Math.max(...values);
      const span = max - min || 1;
      const py = v => PAD + (1 - (v - min) / span) * (H - 2 * PAD);
      const points = trend.map((p, i) => `${px(i).toFixed(1)},${py(p[key]).toFixed(1)}`).join(' ');
      const range = min === max ? `${min}` : `${min}–${max}`;
      return `<div class="bed-stats__series-label">${label}<span class="bed-stats__series-range">${range}</span></div>
      <svg class="bed-stats__spark" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" role="img" aria-label="${label} 趨勢">
        <polyline points="${points}" fill="none" stroke="${color}" stroke-width="1.5" vector-effect="non-scaling-stroke" />
      </svg>`;
    };
    return chart('bpm', 'BPM', 'var(--amber)') + chart('rpm', 'RPM', 'var(--teal)');
  }

  function _renderStats(res) {
    const el = document.getElementById('bed-drawer-stats');
    if (!el) return;
    if (res.status !== 'success') {
      el.innerHTML = `<p class="bed-stats__empty">統計載入失敗：${res.message ?? '未知'}</p>`;
      return;
    }
    const s = res.stats;
    const pct = s.success_rate == null ? '--' : `${Math.round(s.success_rate * 100)}%`;
    el.innerHTML = `<div class="bed-stats__head">
      <span class="bed-stats__title" id="bed-drawer-stats-title">近 ${s.window} 次有效量測</span>
      <select class="bed-stats__window" id="bed-drawer-window">
        ${WINDOW_OPTIONS.map(n => `<option value="${n}"${n === s.window ? ' selected' : ''}>${n}</option>`).join('')}
      </select>
    </div>
    <div class="bed-stats__grid">
      <div class="bed-stat"><span class="bed-stat__label">平均 BPM</span><span class="bed-stat__value" id="bed-stat-bpm">${s.avg_bpm ?? '--'}</span></div>
      <div class="bed-stat"><span class="bed-stat__label">平均 RPM</span><span class="bed-stat__value" id="bed-stat-rpm">${s.avg_rpm ?? '--'}</span></div>
      <div class="bed-stat"><span class="bed-stat__label">成功率</span><span class="bed-stat__value">${pct}</span></div>
      <div class="bed-stat"><span class="bed-stat__label">有效次數</span><span class="bed-stat__value">${s.valid_count}</span></div>
    </div>
    ${_renderSparkline(res.trend)}`;
    document.getElementById('bed-drawer-window').onchange = (ev) => {
      const win = parseInt(ev.target.value, 10);
      localStorage.setItem('bedStatsWindow', win);
      dataService.getBedStats(_drawer.locationId, win).then(_renderStats);
    };
  }

  function _renderRows() {
    const body = document.getElementById('bed-drawer-body');
    if (!body) return;
    const perRun = dedupeScans([..._drawer.rows.values()]);
    if (!perRun.length) {
      body.innerHTML = '<p style="color:var(--text-muted);font-size:11px;">尚無掃描記錄</p>';
      return;
    }
    body.innerHTML = perRun.map(d => `
      <div class="drawer-row drawer-row--${d.is_valid ? 'valid' : 'invalid'}">
        <div class="drawer-row__time">${new Date(d.timestamp).toLocaleString()}</div>
        <div class="drawer-row__vit">BPM ${d.bpm ?? '--'} · RPM ${d.rpm ?? '--'} · status=${d.status ?? '--'}</div>
        ${d.details ? `<div class="drawer-row__detail">${d.details}</div>` : ''}
      </div>
    `).join('') + (_drawer.done ? '<p class="drawer-hint" id="bed-drawer-end">已載入全部</p>' : '');
  }

  function _mergeRows(rows) {
    rows.forEach(r => { if (!_drawer.rows.has(r.id)) _drawer.rows.set(r.id, r); });
    if (_drawer.rows.size) _drawer.oldestId = Math.min(...[..._drawer.rows.keys()]);
  }

  function _loadMore() {
    _drawer.loading = true;
    dataService.getScanHistoryByLocation(_drawer.locationId, PAGE_SIZE, _drawer.oldestId).then(res => {
      _drawer.loading = false;
      if (res.status !== 'success') return;
      _mergeRows(res.data);
      if (res.data.length < PAGE_SIZE) _drawer.done = true;
      _renderRows();
    }).catch(() => { _drawer.loading = false; });
  }

  function _onDrawerScroll(ev) {
    if (_drawer.loading || _drawer.done) return;
    const body = ev.currentTarget;
    if (body.scrollHeight - body.scrollTop - body.clientHeight < 200) _loadMore();
  }

  function openDrawer(bedKey) {
    const drawer = document.getElementById('bed-drawer');
    const title = document.getElementById('bed-drawer-title');
    const body = document.getElementById('bed-drawer-body');
    if (!drawer || !title || !body) return;

    // Resolve bedKey → location_id (canonical key) via cached beds config
    const bed = _bedsCache?.beds?.[bedKey];
    const locationId = bed?.location_id;
    if (!locationId) {
      console.warn(`openDrawer: bedKey ${bedKey} has no location_id in beds.json`);
      return;
    }

    title.textContent = `Bed ${bedKey}`;
    body.innerHTML = '<p style="color:var(--text-muted);font-size:11px;">Loading…</p>';
    document.getElementById('bed-drawer-stats').innerHTML = '';
    drawer.removeAttribute('hidden');

    _drawer.locationId = locationId;
    _drawer.rows = new Map();
    _drawer.oldestId = null;
    _drawer.loading = false;
    _drawer.done = false;

    dataService.getBedStats(locationId, _statsWindow())
      .then(_renderStats)
      .catch(err => _renderStats({ status: 'error', message: err.message }));

    // Raw rows, not runs: a failed scan writes one row per retry, so the page
    // is deduped to one outcome row per patrol run before rendering.
    dataService.getScanHistoryByLocation(locationId, PAGE_SIZE).then(res => {
      if (res.status !== 'success') {
        body.innerHTML = `<p style="color:var(--accent-red);font-size:11px;">載入失敗：${res.message ?? '未知'}</p>`;
        return;
      }
      _mergeRows(res.data);
      if (res.data.length < PAGE_SIZE) _drawer.done = true;
      _renderRows();
    }).catch(err => {
      body.innerHTML = `<p style="color:var(--accent-red);font-size:11px;">載入失敗：${err.message}</p>`;
    });

    if (_drawer.scrollHandler) body.removeEventListener('scroll', _drawer.scrollHandler);
    _drawer.scrollHandler = _onDrawerScroll;
    body.addEventListener('scroll', _drawer.scrollHandler);

    // Backdrop click + ESC close
    const backdrop = drawer.querySelector('.bed-drawer-backdrop');
    if (backdrop) backdrop.onclick = closeDrawer;
    const closeBtn = document.getElementById('bed-drawer-close');
    if (closeBtn) closeBtn.onclick = closeDrawer;
    // Drop any previous handler so re-opening doesn't accumulate listeners
    if (drawer._escHandler) {
      document.removeEventListener('keydown', drawer._escHandler);
    }
    drawer._escHandler = (e) => { if (e.key === 'Escape') closeDrawer(); };
    document.addEventListener('keydown', drawer._escHandler);
  }

  function closeDrawer() {
    const drawer = document.getElementById('bed-drawer');
    if (!drawer) return;
    drawer.setAttribute('hidden', '');
    if (drawer._escHandler) {
      document.removeEventListener('keydown', drawer._escHandler);
      drawer._escHandler = null;
    }
    const body = document.getElementById('bed-drawer-body');
    if (body && _drawer.scrollHandler) body.removeEventListener('scroll', _drawer.scrollHandler);
    _drawer.scrollHandler = null;
    _drawer.locationId = null;
    _drawer.rows = new Map();
    _drawer.oldestId = null;
    _drawer.loading = false;
    _drawer.done = false;
  }

  global.bedGrid = {
    init,
    teardown,
    refreshConfig,
    classifyBedState,
    formatRelativeTime,
    renderBedCard,
    renderBedGrid,
    toggleRoom,
    openDrawer,
    closeDrawer,
  };
})(window);
