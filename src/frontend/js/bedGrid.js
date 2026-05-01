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

  global.bedGrid = {
    init() {},
    teardown() {},
    classifyBedState,
    formatRelativeTime() { return '--'; },
  };
})(window);
