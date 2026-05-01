// IT-9 Slice 2: bed-grid + drawer logic. Empty in Slice 0; populated in Slice 2.
(function (global) {
  global.bedGrid = {
    init() {},
    teardown() {},
    classifyBedState() { return 'unscheduled'; },
    formatRelativeTime() { return '--'; },
  };
})(window);
