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

  global.bedGrid = {
    init() {},
    teardown() {},
    classifyBedState,
    formatRelativeTime,
  };
})(window);
