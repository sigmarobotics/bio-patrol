// IT-9 Slice 2: bed-grid + drawer logic.
(function (global) {
  function classifyBedState(bed, latest, isPatrolEnabled, staleHours, now = Date.now()) {
    if (!isPatrolEnabled) return 'unscheduled';
    if (!latest) return 'stale';
    if (!latest.is_valid) return 'invalid';
    const ts = Date.parse(latest.timestamp);
    if (Number.isNaN(ts)) return 'stale';
    const ageMs = now - ts;
    const thresholdMs = staleHours * 3600 * 1000;
    return ageMs > thresholdMs ? 'stale' : 'valid';
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

    if (state === 'unscheduled') {
      ts = '未排程';
    } else if (state === 'invalid' && latest) {
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
    const latestByLocId = {};
    (latestByBedList || []).forEach(r => { latestByLocId[r.location_id] = r; });

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
          latestByLocId[b.location_id],
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
  let _staleHoursCache = 24;        // cached at init; only refreshed when settings change
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
    _staleHoursCache = settings?.bed_card_stale_hours ?? 24;
  }

  async function init() {
    if (_pollState.intervalId) return; // already running
    await _refreshConfig();
    await loadBedGrid();
    _pollState.intervalId = setInterval(() => {
      if (!document.hidden) loadBedGrid();
    }, 10000);
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

  global.bedGrid = {
    init,
    teardown,
    refreshConfig,
    classifyBedState,
    formatRelativeTime,
    renderBedCard,
    renderBedGrid,
    toggleRoom,
    openDrawer() {},   // populated in Task 2.7
    closeDrawer() {},  // populated in Task 2.7
  };
})(window);
