// ═══════════════════════════════════════════════════════════════════════════
// KACHAKA CARE // COMMAND CENTER — Main Application Script
// Single robot (robot_id = "kachaka"), 5-tab SPA
// ═══════════════════════════════════════════════════════════════════════════

// --- Global State ---
let tasks = [];
let currentTab = 'dashboard';
let pollingInterval = null;
let robotData = { battery: null, pose: null, status: 'unknown' };
let shelfDropPose = null;  // {x, y, theta} or null — set by checkShelfDrop()
let _cancelledDismissed = new Set();  // task IDs dismissed after showing "cancelled"
let _cancelHideTimer = null;

// IT-9: gMapDesc + tfROS2Canvas moved to mapView.js (use mapView.getState() if needed)

// ═══════════════════════════════════════════════════════════════════════════
// TAB NAVIGATION
// ═══════════════════════════════════════════════════════════════════════════

function switchTab(tabName) {
  const prevTab = currentTab;  // IT-9: capture before switch
  currentTab = tabName;

  // Update tab buttons
  document.querySelectorAll('.tab-btn').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.tab === tabName);
  });

  // Update tab content
  document.querySelectorAll('.tab-content').forEach(view => {
    view.classList.toggle('active', view.id === `view-${tabName}`);
  });

  // Load tab-specific data on switch
  switch (tabName) {
    case 'dashboard': loadDashboardData(); break;
    case 'patrol': loadPatrolConfig(); break;
    case 'beds': loadBedsConfig(); break;
    case 'sensor': loadSensorData(); break;
    case 'settings': loadSettings(); break;
  }
  // Stop the buttons-panel polling when leaving settings.
  if (tabName !== 'settings' && typeof window.unloadButtons === 'function') {
    window.unloadButtons();
  }

  // IT-9: stop dashboard polling when leaving dashboard
  if (prevTab === 'dashboard' && tabName !== 'dashboard') {
    if (window.bedGrid?.teardown) bedGrid.teardown();
  }
}

// ═══════════════════════════════════════════════════════════════════════════
// COLLAPSIBLE MONITOR FRAMES
// ═══════════════════════════════════════════════════════════════════════════

function toggleFrame(frameId) {
  const frame = document.getElementById(frameId);
  if (!frame) return;
  frame.classList.toggle('collapsed');
  const icon = frame.querySelector('.toggle-icon');
  if (icon) icon.textContent = frame.classList.contains('collapsed') ? '▶' : '▼';
}

// ═══════════════════════════════════════════════════════════════════════════
// HEADER CLOCK
// ═══════════════════════════════════════════════════════════════════════════

let _cachedTimezone = null;

function updateHeaderClock() {
  const el = document.getElementById('header-clock');
  if (!el) return;
  // Read timezone from the setting select (live), with cached fallback
  const tzSelect = document.getElementById('setting-timezone');
  if (tzSelect && tzSelect.value) _cachedTimezone = tzSelect.value;
  const tz = _cachedTimezone || 'Asia/Taipei';
  try {
    el.textContent = new Date().toLocaleTimeString('en-GB', { timeZone: tz, hour12: false });
  } catch {
    el.textContent = new Date().toLocaleTimeString('en-GB', { hour12: false });
  }
}

// ═══════════════════════════════════════════════════════════════════════════
// INITIALIZATION
// ═══════════════════════════════════════════════════════════════════════════

window.addEventListener('DOMContentLoaded', async () => {
  // Bind tab buttons
  document.querySelectorAll('.tab-btn').forEach(btn => {
    btn.addEventListener('click', () => switchTab(btn.dataset.tab));
  });

  // Bind button events
  const btnHome = document.getElementById('btn-home');
  if (btnHome) btnHome.addEventListener('click', returnHome);

  const btnCancel = document.getElementById('btn-cancel-command');
  if (btnCancel) btnCancel.addEventListener('click', returnHome);

  // Initialize map (IT-9: via mapView module; animation loop starts internally)
  if (window.mapView) {
    mapView.init('map-canvas', { interactive: true });
  }

  // Load initial data
  await loadDashboardData();

  // Start polling
  startPolling();

  // Start header clock
  updateHeaderClock();
  setInterval(updateHeaderClock, 1000);
});

// ═══════════════════════════════════════════════════════════════════════════
// DATA POLLING
// ═══════════════════════════════════════════════════════════════════════════

function startPolling() {
  if (pollingInterval) clearInterval(pollingInterval);
  pollingInterval = setInterval(async () => {
    try {
      await Promise.all([
        fetchRobotStatus(),
        fetchTaskStatus(),
      ]);
      // Check for shelf-drop
      checkShelfDrop();
    } catch (e) {
      console.error('Polling error:', e);
    }
  }, 2000);
}

async function fetchRobotStatus() {
  try {
    const [batteryRes, poseRes] = await Promise.all([
      dataService.getRobotBattery(),
      dataService.getRobotPose(),
    ]);

    // Battery
    const battery = batteryRes?.remaining_percentage;
    if (battery !== undefined) {
      robotData.battery = battery;
      const el = document.getElementById('battery-value');
      if (el) el.textContent = `${Math.round(battery)}%`;
    }

    // Pose
    const pose = poseRes;
    if (pose && pose.x !== undefined) {
      robotData.pose = pose;
      const poseEl = document.getElementById('pose-display');
      if (poseEl) {
        poseEl.textContent = `X: ${pose.x.toFixed(2)} Y: ${pose.y.toFixed(2)} θ: ${(pose.theta || 0).toFixed(2)}`;
      }
    }

    // Update connection indicator
    robotData.status = 'online';
    const connEl = document.getElementById('connection-status');
    if (connEl) {
      connEl.classList.remove('disconnected');
      connEl.classList.add('connected');
    }
  } catch (e) {
    robotData.status = 'offline';
    const connEl = document.getElementById('connection-status');
    if (connEl) {
      connEl.classList.remove('connected');
      connEl.classList.add('disconnected');
    }
  }
}

async function fetchTaskStatus() {
  try {
    const response = await dataService.getTasks();
    const tasksData = Array.isArray(response) ? response : (response?.data || []);
    tasks = tasksData;
    updatePatrolProgress();
  } catch (e) {
    console.error('Failed to fetch tasks:', e);
  }
}

function updatePatrolProgress() {
  const container = document.getElementById('patrol-progress');
  const cancelBtn = document.getElementById('btn-cancel-patrol');
  if (!container) return;

  // Find active or most recent patrol task
  const activeTask = tasks.find(t => t.status === 'in_progress' || t.status === 'queued');
  const recentDone = !activeTask ? tasks.find(t =>
    t.status === 'done' || t.status === 'failed' ||
    (t.status === 'cancelled' && !_cancelledDismissed.has(t.task_id))
  ) : null;
  const task = activeTask || recentDone;

  if (!task || !task.steps) {
    container.style.display = 'none';
    if (cancelBtn) cancelBtn.style.display = 'none';
    return;
  }

  const bioSteps = task.steps.filter(s => s.action === 'bio_scan' || s.action === 'wait');
  if (bioSteps.length === 0) {
    container.style.display = 'none';
    if (cancelBtn) cancelBtn.style.display = 'none';
    return;
  }

  const total = bioSteps.length;
  const completed = bioSteps.filter(s => s.status === 'success' || s.status === 'fail' || s.status === 'skipped').length;
  const pct = total > 0 ? Math.round((completed / total) * 100) : 0;

  document.getElementById('patrol-progress-count').textContent = `${completed} / ${total}`;

  const bar = document.getElementById('patrol-progress-bar');
  bar.style.width = `${pct}%`;
  bar.classList.toggle('done', task.status === 'done');

  const statusEl = document.getElementById('patrol-progress-status');
  const executing = bioSteps.find(s => s.status === 'executing');

  // Show/hide cancel button
  const isActive = task.status === 'in_progress' || task.status === 'queued';
  if (cancelBtn) cancelBtn.style.display = isActive ? '' : 'none';

  if (task.status === 'in_progress' && executing) {
    statusEl.textContent = executing.params?.bed_key ? `Scanning ${executing.params.bed_key}...` : 'Scanning...';
  } else if (isActive) {
    statusEl.textContent = 'In progress...';
  } else if (task.status === 'cancelled') {
    statusEl.textContent = 'Patrol cancelled';
    // Auto-hide progress bar after a short delay (guard against repeated timers)
    if (!_cancelHideTimer) {
      _cancelHideTimer = setTimeout(() => {
        _cancelledDismissed.add(task.task_id);
        container.style.display = 'none';
        _cancelHideTimer = null;
      }, 3000);
    }
  } else if (task.status === 'done') {
    statusEl.textContent = 'Completed';
  } else if (task.status === 'failed') {
    statusEl.textContent = 'Failed';
  } else {
    statusEl.textContent = '';
  }

  container.style.display = '';
}

// ═══════════════════════════════════════════════════════════════════════════
// SHELF DROP DETECTION & RECOVERY
// ═══════════════════════════════════════════════════════════════════════════

function checkShelfDrop() {
  const shelfDropTask = tasks.find(t => t.status === 'shelf_dropped');
  const overlay = document.getElementById('shelf-drop-overlay');
  if (!overlay) return;

  if (shelfDropTask) {
    const meta = shelfDropTask.metadata || {};
    shelfDropPose = meta.shelf_pose || null;

    // Draw mini-map with shelf drop marker
    drawShelfDropMiniMap();

    // Show remaining beds
    const remainingEl = document.getElementById('shelf-drop-remaining');
    const remaining = meta.remaining_beds || [];
    if (remainingEl) {
      if (remaining.length > 0) {
        remainingEl.innerHTML = '<p style="margin:0 0 6px;font-size:13px;color:var(--text-muted);">尚未巡房的床位：</p>' +
          remaining.map(b => `<span class="bed-chip">${b.bed_key}</span>`).join('');
        remainingEl.style.display = 'block';
      } else {
        remainingEl.style.display = 'none';
      }
    }

    // Show/hide resume button based on remaining beds
    const resumeBtn = document.getElementById('btn-resume-patrol');
    if (resumeBtn) resumeBtn.style.display = remaining.length > 0 ? '' : 'none';

    // Store task ID for recovery/resume
    overlay.dataset.taskId = shelfDropTask.task_id;
    overlay.style.display = 'flex';
  } else {
    overlay.style.display = 'none';
    shelfDropPose = null;
  }
}

function drawShelfDropMiniMap() {
  const canvas = document.getElementById('shelf-drop-map-canvas');
  const mvState = window.mapView?.getState?.();
  if (!canvas || !mvState || !mvState.img) return;
  const { img, gMapDesc, tfROS2Canvas } = mvState;

  const wrap = canvas.parentElement;
  canvas.width = wrap.clientWidth;
  canvas.height = wrap.clientHeight;

  const ctx = canvas.getContext('2d');
  ctx.clearRect(0, 0, canvas.width, canvas.height);

  // Fit map into canvas with padding
  const pad = 10;
  const scaleX = (canvas.width - pad * 2) / gMapDesc.w;
  const scaleY = (canvas.height - pad * 2) / gMapDesc.h;
  const scale = Math.min(scaleX, scaleY);
  const offX = (canvas.width - gMapDesc.w * scale) / 2;
  const offY = (canvas.height - gMapDesc.h * scale) / 2;

  ctx.save();
  ctx.translate(offX, offY);
  ctx.scale(scale, scale);

  // Draw map image
  ctx.drawImage(img, 0, 0, gMapDesc.w, gMapDesc.h);

  // Draw shelf drop marker
  if (shelfDropPose) {
    const dropPos = tfROS2Canvas(gMapDesc, shelfDropPose);
    if (dropPos.x && dropPos.y) {
      ctx.save();
      ctx.translate(dropPos.x, dropPos.y);

      // Outer glow
      ctx.beginPath();
      ctx.arc(0, 0, 14, 0, 2 * Math.PI);
      ctx.fillStyle = 'rgba(255, 0, 0, 0.2)';
      ctx.fill();

      // Red circle
      ctx.beginPath();
      ctx.arc(0, 0, 8, 0, 2 * Math.PI);
      ctx.fillStyle = 'rgba(255, 0, 0, 0.5)';
      ctx.fill();
      ctx.beginPath();
      ctx.arc(0, 0, 4, 0, 2 * Math.PI);
      ctx.fillStyle = 'red';
      ctx.fill();

      // White cross
      ctx.strokeStyle = '#fff';
      ctx.lineWidth = 2;
      ctx.beginPath();
      ctx.moveTo(-3, -3); ctx.lineTo(3, 3);
      ctx.moveTo(3, -3);  ctx.lineTo(-3, 3);
      ctx.stroke();

      ctx.restore();
    }
  }

  ctx.restore();
}

async function recoverShelf() {
  const statusEl = document.getElementById('shelf-recovery-status');
  if (statusEl) statusEl.textContent = '歸位中...';

  try {
    const settings = await dataService.getSettings();
    const shelfId = settings?.shelf_id || 'S_04';

    const result = await dataService.recoverShelf(shelfId);
    if (statusEl) statusEl.textContent = '歸位成功！';
    setTimeout(() => {
      document.getElementById('shelf-drop-overlay').style.display = 'none';
      if (statusEl) statusEl.textContent = '';
    }, 2000);
  } catch (e) {
    if (statusEl) statusEl.textContent = `歸位失敗: ${e.message || e}`;
  }
}

async function resumePatrol() {
  const overlay = document.getElementById('shelf-drop-overlay');
  const taskId = overlay?.dataset.taskId;
  if (!taskId) return;

  const statusEl = document.getElementById('shelf-recovery-status');
  if (statusEl) statusEl.textContent = '歸位並恢復巡房中...';

  try {
    const result = await dataService.resumePatrol(taskId);
    if (statusEl) statusEl.textContent = `已恢復巡房，剩餘 ${result.beds_count} 床`;
    setTimeout(() => {
      overlay.style.display = 'none';
      if (statusEl) statusEl.textContent = '';
    }, 2000);
  } catch (e) {
    if (statusEl) statusEl.textContent = `恢復失敗: ${e.response?.data?.detail || e.message || e}`;
  }
}

// ═══════════════════════════════════════════════════════════════════════════
// DASHBOARD
// ═══════════════════════════════════════════════════════════════════════════

async function loadDashboardData() {
  // IT-9: bed-grid + robot mini frame (slices 2 + 3 fill these stubs)
  if (window.bedGrid?.init) bedGrid.init();
  if (window.mapView?.initMini) mapView.initMini();
  loadScheduleForDashboard();
}

async function loadScheduleForDashboard() {
  try {
    scheduleConfig = await dataService.getSchedules();
    renderScheduleList();
    updateNextRunDisplay();
  } catch (e) {
    console.error('Failed to load schedules:', e);
  }
}

function computeNextRun(schedules) {
  const now = new Date();
  let nearest = null;

  for (const s of schedules) {
    if (!s.enabled || !s.time) continue;
    const [h, m] = s.time.split(':').map(Number);

    for (let d = 0; d < 7; d++) {
      const candidate = new Date(now);
      candidate.setDate(candidate.getDate() + d);
      candidate.setHours(h, m, 0, 0);

      if (candidate <= now) continue;

      const dow = candidate.getDay();
      if (s.type === 'weekday' && (dow === 0 || dow === 6)) continue;

      if (!nearest || candidate < nearest) {
        nearest = candidate;
      }
      break;
    }
  }
  return nearest;
}

function updateNextRunDisplay() {
  const el = document.getElementById('next-run-time');
  if (!el) return;
  const schedules = scheduleConfig?.schedules || [];
  const next = computeNextRun(schedules);
  if (next) {
    const hh = String(next.getHours()).padStart(2, '0');
    const mm = String(next.getMinutes()).padStart(2, '0');
    const isToday = next.toDateString() === new Date().toDateString();
    el.textContent = isToday ? `Today ${hh}:${mm}` : `${next.toLocaleDateString('en', {weekday:'short'})} ${hh}:${mm}`;
  } else {
    el.textContent = '--';
  }
}

async function loadLatestBioSensor() {
  try {
    const res = await dataService.getLatestBioSensor();
    if (res.status === 'success' && res.data) {
      const d = res.data;
      const bpmEl = document.getElementById('bio-bpm');
      const rpmEl = document.getElementById('bio-rpm');
      const statusEl = document.getElementById('bio-status');
      if (bpmEl) bpmEl.textContent = d.bpm || '--';
      if (rpmEl) rpmEl.textContent = d.rpm || '--';
      if (statusEl) statusEl.textContent = d.status || '--';
    }
  } catch (e) {
    console.error('Failed to load bio sensor:', e);
  }
}

// Quick actions
async function resetShelfSensor() {
  try {
    const settings = await dataService.getSettings();
    const shelfId = settings?.shelf_id || 'S_04';
    await dataService.resetShelfPose(shelfId);
    alert('生理感測器歸位成功');
  } catch (e) {
    alert('歸位失敗: ' + (e.message || e));
  }
}

async function returnHome() {
  try {
    await dataService.returnHome();
  } catch (e) {
    console.error('Return home failed:', e);
  }
}

async function startDemoRun() {
  try {
    // Defensively return shelf to home before starting
    try { await _returnShelfQuiet(); } catch (_) {}
    const res = await dataService.startPatrol('demo');
    alert('Demo Run started!');
  } catch (e) {
    alert('Failed to start demo run: ' + (e.message || e));
  }
}

async function startPatrol() {
  try {
    // Defensively return shelf to home before starting
    try { await _returnShelfQuiet(); } catch (_) {}
    const res = await dataService.startPatrol('patrol');
    alert('Patrol started!');
  } catch (e) {
    alert('Failed to start patrol: ' + (e.message || e));
  }
}

async function cancelPatrol() {
  const active = tasks.find(t => t.status === 'in_progress' || t.status === 'queued');
  if (!active) return;
  try {
    await dataService.cancelTask(active.task_id);
  } catch (e) {
    console.error('Cancel patrol failed:', e);
  }
}

async function _returnShelfQuiet() {
  const settings = await dataService.getSettings();
  const shelfId = settings?.shelf_id || 'S_04';
  await dataService.resetShelfPose(shelfId);
}

// Manual control (D-pad)
async function manualControl(direction) {
  if (!robotData.pose) return;
  const step = 0.1;
  const angleStep = 0.174533; // ~10 degrees
  let { x, y, theta } = robotData.pose;
  theta = theta || 0;

  switch (direction) {
    case 'forward':
      x += step * Math.cos(theta);
      y += step * Math.sin(theta);
      break;
    case 'backward':
      x -= step * Math.cos(theta);
      y -= step * Math.sin(theta);
      break;
    case 'left': theta += angleStep; break;
    case 'right': theta -= angleStep; break;
  }

  try {
    await dataService.moveToPose(x, y, theta);
  } catch (e) {
    console.error('Manual control failed:', e);
  }
}

// ═══════════════════════════════════════════════════════════════════════════
// MAP RENDERING — extracted to mapView.js (IT-9 Slice 0)
// ═══════════════════════════════════════════════════════════════════════════
// All map state/init/drawing/pan-zoom/animation lives in js/mapView.js.
// Use window.mapView.{init, refreshPose, getState, destroy}.

// ═══════════════════════════════════════════════════════════════════════════
// PATROL TAB
// ═══════════════════════════════════════════════════════════════════════════

let patrolConfig = null;
let bedsConfig = null;
let scheduleConfig = null;

async function loadPatrolConfig() {
  try {
    const [patrol, beds, schedule] = await Promise.all([
      dataService.getPatrol(),
      dataService.getBeds(),
      dataService.getSchedules(),
    ]);
    patrolConfig = patrol;
    bedsConfig = beds;
    scheduleConfig = schedule;

    // Render schedule list
    renderScheduleList();

    // Render patrol route
    renderPatrolRoute();

    // Load preset dropdown
    refreshPatrolPresets();
  } catch (e) {
    console.error('Failed to load patrol config:', e);
  }
}

function renderScheduleList() {
  const container = document.getElementById('schedule-list');
  if (!container || !scheduleConfig) return;

  const schedules = scheduleConfig.schedules || [];
  if (schedules.length === 0) {
    container.innerHTML = '<p style="color:var(--text-muted);font-size:12px;">No schedules configured</p>';
    return;
  }

  container.innerHTML = schedules.map(s => `
    <div class="schedule-item">
      <input type="checkbox" ${s.enabled ? 'checked' : ''} onchange="toggleSchedule('${s.id}', this.checked)">
      <span class="schedule-time">${s.time}</span>
      <span class="schedule-type">${s.type === 'weekday' ? 'Weekdays' : 'Daily'}</span>
      <button class="remove-btn" onclick="removeSchedule('${s.id}')" title="Remove">✕</button>
    </div>
  `).join('');
}

async function addSchedule() {
  const timeInput = document.getElementById('new-schedule-time');
  const typeSelect = document.getElementById('new-schedule-type');
  if (!timeInput || !timeInput.value) return;

  const newSchedule = {
    id: 'sched-' + Date.now(),
    enabled: true,
    time: timeInput.value,
    type: typeSelect ? typeSelect.value : 'daily',
  };

  if (!scheduleConfig) scheduleConfig = { schedules: [] };
  scheduleConfig.schedules.push(newSchedule);

  try {
    await dataService.saveSchedules(scheduleConfig);
    renderScheduleList();
    updateNextRunDisplay();
    timeInput.value = '';
  } catch (e) {
    alert('Failed to save schedule: ' + e.message);
  }
}

async function removeSchedule(scheduleId) {
  try {
    await dataService.deleteSchedule(scheduleId);
    if (scheduleConfig) {
      scheduleConfig.schedules = scheduleConfig.schedules.filter(s => s.id !== scheduleId);
    }
    renderScheduleList();
    updateNextRunDisplay();
  } catch (e) {
    alert('Failed to remove schedule: ' + e.message);
  }
}

async function toggleSchedule(scheduleId, enabled) {
  if (!scheduleConfig) return;
  const s = scheduleConfig.schedules.find(s => s.id === scheduleId);
  if (s) s.enabled = enabled;
  try {
    await dataService.saveSchedules(scheduleConfig);
    updateNextRunDisplay();
  } catch (e) {
    console.error('Failed to toggle schedule:', e);
  }
}

function renderPatrolRoute() {
  const container = document.getElementById('patrol-route-list');
  if (!container || !patrolConfig || !bedsConfig) return;

  const bedsOrder = patrolConfig.beds_order || [];
  const bedsMap = bedsConfig.beds || {};

  // Build a lookup: bed_key → entry in beds_order
  const orderLookup = {};
  bedsOrder.forEach(entry => {
    orderLookup[entry.bed_key] = entry;
  });

  // Group ALL beds from bedsConfig by room
  const roomGroups = {};
  Object.keys(bedsMap).forEach(bedKey => {
    const bed = bedsMap[bedKey];
    const room = bed.room || bedKey.split('-')[0];
    if (!roomGroups[room]) roomGroups[room] = [];
    roomGroups[room].push({ bed_key: bedKey, ...bed });
  });

  let html = '';
  Object.keys(roomGroups).sort((a, b) => parseInt(a) - parseInt(b)).forEach(room => {
    const beds = roomGroups[room];
    // Sort beds within room by bed number
    beds.sort((a, b) => (a.bed || 0) - (b.bed || 0));

    // Count enabled beds in this room
    const enabledCount = beds.filter(b => orderLookup[b.bed_key]?.enabled).length;
    const roomLabel = `Room ${room}`;
    const countLabel = enabledCount > 0 ? `${enabledCount}/${beds.length}` : '';

    const bedKeys = beds.map(b => b.bed_key);
    const bedKeysJson = JSON.stringify(bedKeys).replace(/"/g, '&quot;');

    html += `<div class="patrol-room">
      <div class="patrol-room-header">
        <span class="room-label" onclick="this.closest('.patrol-room').classList.toggle('collapsed')">${roomLabel}</span>
        <span class="room-count">${countLabel}</span>
        <button class="room-btn room-btn-all" onclick="event.stopPropagation();setRoomBeds(${bedKeysJson}, true)" title="Select all">All</button>
        <button class="room-btn room-btn-none" onclick="event.stopPropagation();setRoomBeds(${bedKeysJson}, false)" title="Deselect all">None</button>
        <span class="toggle-icon" onclick="this.closest('.patrol-room').classList.toggle('collapsed')">▼</span>
      </div>
      <div class="patrol-bed-list">`;

    beds.forEach(bed => {
      const inOrder = orderLookup[bed.bed_key];
      const isEnabled = inOrder?.enabled || false;
      const locLabel = bed.location_id || 'no location';
      html += `<div class="patrol-bed-item ${isEnabled ? 'enabled' : ''}"
                    onclick="togglePatrolBed('${bed.bed_key}')">
        <span class="bed-toggle">${isEnabled ? '✓' : ''}</span>
        <span class="bed-label">${bed.bed_key}</span>
        <span class="bed-location">${locLabel}</span>
      </div>`;
    });

    html += `</div></div>`;
  });

  container.innerHTML = html || '<p style="color:var(--text-muted);font-size:12px;">No beds configured. Set up beds in the Beds tab first.</p>';
}

function togglePatrolBed(bedKey) {
  if (!patrolConfig) return;
  if (!patrolConfig.beds_order) patrolConfig.beds_order = [];

  const idx = patrolConfig.beds_order.findIndex(e => e.bed_key === bedKey);
  if (idx >= 0) {
    patrolConfig.beds_order[idx].enabled = !patrolConfig.beds_order[idx].enabled;
  } else {
    patrolConfig.beds_order.push({ bed_key: bedKey, enabled: true });
  }
  renderPatrolRoute();
  autoSavePatrolConfig();
}

function setRoomBeds(bedKeys, enabled) {
  if (!patrolConfig) return;
  if (!patrolConfig.beds_order) patrolConfig.beds_order = [];

  bedKeys.forEach(bedKey => {
    const idx = patrolConfig.beds_order.findIndex(e => e.bed_key === bedKey);
    if (idx >= 0) {
      patrolConfig.beds_order[idx].enabled = enabled;
    } else if (enabled) {
      patrolConfig.beds_order.push({ bed_key: bedKey, enabled: true });
    }
  });
  renderPatrolRoute();
  autoSavePatrolConfig();
}

let _autoSaveTimer = null;
function autoSavePatrolConfig() {
  if (_autoSaveTimer) clearTimeout(_autoSaveTimer);
  _autoSaveTimer = setTimeout(async () => {
    if (!patrolConfig) return;
    try {
      await dataService.savePatrol(patrolConfig);
    } catch (e) {
      console.error('Auto-save patrol failed:', e);
    }
  }, 500);
}

async function savePatrolConfig() {
  if (!patrolConfig) return;

  const toast = document.getElementById('patrol-save-toast');
  try {
    await dataService.savePatrol(patrolConfig);
    if (toast) {
      toast.textContent = 'Saved!';
      toast.className = 'save-toast show';
      setTimeout(() => { toast.className = 'save-toast'; }, 2000);
    }
  } catch (e) {
    if (toast) {
      toast.textContent = 'Failed: ' + e.message;
      toast.className = 'save-toast show error';
      setTimeout(() => { toast.className = 'save-toast'; }, 3000);
    }
  }
}

// --- Patrol presets ---

async function refreshPatrolPresets() {
  const sel = document.getElementById('patrol-preset-select');
  if (!sel) return;

  try {
    const res = await dataService.getPatrolPresets();
    const presets = res.presets || [];
    const demo = res.demo_preset || '';
    const prev = sel.value;
    sel.innerHTML = '<option value="">-- Presets --</option>' +
      presets.map(p => {
        const isDemo = p.name === demo;
        const label = isDemo ? `${p.name} (${p.beds_count} beds) [DEMO]` : `${p.name} (${p.beds_count} beds)`;
        return `<option value="${p.name}">${label}</option>`;
      }).join('');
    if (prev) sel.value = prev;
  } catch (e) {
    console.error('Failed to load presets:', e);
  }
}

async function savePatrolPreset() {
  const name = prompt('Preset name:');
  if (!name || !name.trim()) return;
  if (!patrolConfig) return;

  try {
    await dataService.savePatrol(patrolConfig);
    await dataService.savePatrolPreset(name.trim());
    await refreshPatrolPresets();
    alert(`Saved as "${name.trim()}"`);
  } catch (e) {
    alert('Failed to save preset: ' + (e.response?.data?.detail || e.message));
  }
}

async function onPresetSelect(name) {
  if (!name) return;
  try {
    const res = await dataService.loadPatrolPreset(name);
    patrolConfig = res.data;
    renderPatrolRoute();
  } catch (e) {
    alert('Failed to load preset: ' + (e.response?.data?.detail || e.message));
  }
}

async function setDemoPreset() {
  const sel = document.getElementById('patrol-preset-select');
  const name = sel?.value;
  if (!name) { alert('Select a preset first'); return; }

  try {
    await dataService.setDemoPreset(name);
    await refreshPatrolPresets();
  } catch (e) {
    alert('Failed to set demo: ' + (e.response?.data?.detail || e.message));
  }
}

async function deletePatrolPreset() {
  const sel = document.getElementById('patrol-preset-select');
  const name = sel?.value;
  if (!name) { alert('Select a preset first'); return; }
  if (!confirm(`Delete preset "${name}"?`)) return;

  try {
    await dataService.deletePatrolPreset(name);
    await refreshPatrolPresets();
  } catch (e) {
    alert('Failed to delete preset: ' + (e.response?.data?.detail || e.message));
  }
}

// ═══════════════════════════════════════════════════════════════════════════
// BEDS TAB
// ═══════════════════════════════════════════════════════════════════════════

async function loadBedsConfig() {
  try {
    bedsConfig = await dataService.getBeds();
    renderBedsUI();
  } catch (e) {
    console.error('Failed to load beds config:', e);
  }
}

function renderBedsUI() {
  if (!bedsConfig) return;

  // Populate form fields
  const countEl = document.getElementById('beds-room-count');
  const startEl = document.getElementById('beds-room-start');
  const numbersEl = document.getElementById('beds-bed-numbers');

  if (countEl) countEl.value = bedsConfig.room_count || 14;
  if (startEl) startEl.value = bedsConfig.room_start || 101;
  if (numbersEl) numbersEl.value = (bedsConfig.bed_numbers || [1, 2, 3, 5, 6]).join(',');

  renderBedsGrid();
}

let robotLocations = [];  // populated by fetchRobotLocations

function renderBedsGrid() {
  const container = document.getElementById('beds-grid-container');
  if (!container || !bedsConfig) return;

  const beds = bedsConfig.beds || {};
  const roomCount = bedsConfig.room_count || 14;
  const roomStart = bedsConfig.room_start || 101;
  const bedNumbers = bedsConfig.bed_numbers || [1, 2, 3, 5, 6];

  let html = '';
  for (let r = 0; r < roomCount; r++) {
    const room = roomStart + r;
    html += `<div class="room-section">
      <h4>Room ${room}</h4>
      <div class="beds-grid">`;

    bedNumbers.forEach(bedNum => {
      const key = `${room}-${bedNum}`;
      const bed = beds[key] || {};
      const currentLoc = bed.location_id || '';

      if (robotLocations.length > 0) {
        // Dropdown mode
        const options = robotLocations.map(loc => {
          const name = loc.name || loc.id || '';
          const selected = name === currentLoc ? 'selected' : '';
          return `<option value="${name}" ${selected}>${name}</option>`;
        }).join('');
        html += `<div class="bed-card">
          <div class="bed-key">${key}</div>
          <select id="bed-loc-${key}" onchange="updateBedLocationId('${key}', this.value)">
            <option value="">-- Select --</option>
            ${options}
          </select>
        </div>`;
      } else {
        // Text input fallback
        html += `<div class="bed-card">
          <div class="bed-key">${key}</div>
          <input type="text" id="bed-loc-${key}" value="${currentLoc}"
                 placeholder="Location ID" onchange="updateBedLocationId('${key}', this.value)">
        </div>`;
      }
    });

    html += `</div></div>`;
  }

  container.innerHTML = html;
}

function updateBedLocationId(bedKey, locationId) {
  if (!bedsConfig) return;
  if (!bedsConfig.beds) bedsConfig.beds = {};
  if (!bedsConfig.beds[bedKey]) {
    const parts = bedKey.split('-');
    bedsConfig.beds[bedKey] = { room: parseInt(parts[0]), bed: parseInt(parts[1]), location_id: locationId };
  } else {
    bedsConfig.beds[bedKey].location_id = locationId;
  }
}

function regenerateBeds() {
  const countEl = document.getElementById('beds-room-count');
  const startEl = document.getElementById('beds-room-start');
  const numbersEl = document.getElementById('beds-bed-numbers');

  const roomCount = parseInt(countEl?.value) || 14;
  const roomStart = parseInt(startEl?.value) || 101;
  const bedNumbers = (numbersEl?.value || '1,2,3,5,6').split(',').map(n => parseInt(n.trim())).filter(n => !isNaN(n));

  const beds = {};
  for (let r = 0; r < roomCount; r++) {
    const room = roomStart + r;
    bedNumbers.forEach(bedNum => {
      const key = `${room}-${bedNum}`;
      beds[key] = {
        room: room,
        bed: bedNum,
        location_id: `B_${key}`
      };
    });
  }

  bedsConfig = { room_count: roomCount, room_start: roomStart, bed_numbers: bedNumbers, beds };
  renderBedsGrid();
}

async function fetchRobotLocations() {
  try {
    const data = await dataService.getRobotLocations();
    const locations = Array.isArray(data) ? data : (data?.locations || []);
    if (!locations.length) {
      alert('No locations registered on the robot.');
      return;
    }

    robotLocations = locations;
    renderBedsGrid();
    alert(`Fetched ${locations.length} locations — select from dropdowns`);
  } catch (e) {
    const detail = e?.response?.data?.detail || e.message;
    alert('Failed to fetch robot locations: ' + detail);
  }
}

async function saveBedsConfig() {
  if (!bedsConfig) return;
  try {
    await dataService.saveBeds(bedsConfig);
    alert('Beds configuration saved!');
  } catch (e) {
    alert('Failed to save beds config: ' + e.message);
  }
}

// ═══════════════════════════════════════════════════════════════════════════
// SENSOR TAB
// ═══════════════════════════════════════════════════════════════════════════

let sensorData = [];

async function loadSensorData() {
  try {
    const dateFilter = document.getElementById('sensor-filter-date')?.value || '';
    const limit = parseInt(document.getElementById('sensor-filter-limit')?.value) || 100;

    const params = { limit };
    // Convert "2026-02-05" → "20260205" prefix to match task_id format
    if (dateFilter) params.task_id = dateFilter.replace(/-/g, '');

    const res = await dataService.getSensorHistory(params);
    sensorData = res.data || [];

    updateSensorStats();
    renderSensorTable();
  } catch (e) {
    console.error('Failed to load sensor data:', e);
  }
}

function clearSensorFilter() {
  const dateEl = document.getElementById('sensor-filter-date');
  if (dateEl) dateEl.value = '';
  loadSensorData();
}

function updateSensorStats() {
  const total = sensorData.length;
  const valid = sensorData.filter(d => d.is_valid).length;
  const rate = total > 0 ? ((valid / total) * 100).toFixed(1) : '0';
  const bpmValues = sensorData.filter(d => d.bpm).map(d => d.bpm);
  const avgBpm = bpmValues.length > 0 ? (bpmValues.reduce((a, b) => a + b, 0) / bpmValues.length).toFixed(0) : '--';

  const el = (id, v) => { const e = document.getElementById(id); if (e) e.textContent = v; };
  el('stat-total-scans', total);
  el('stat-valid-scans', valid);
  el('stat-success-rate', rate + '%');
  el('stat-avg-bpm', avgBpm);
}

function formatTaskId(taskId) {
  if (!taskId) return '--';
  // Parse "YYYYMMDDHHmmSS" → "YYYY/MM/DD HH:mm:SS"
  if (/^\d{14}$/.test(taskId)) {
    return `${taskId.slice(0,4)}/${taskId.slice(4,6)}/${taskId.slice(6,8)} ${taskId.slice(8,10)}:${taskId.slice(10,12)}:${taskId.slice(12,14)}`;
  }
  // Fallback for old UUID-style task_ids
  return taskId.slice(0, 8);
}

function renderSensorTable() {
  const tbody = document.getElementById('sensor-table-body');
  if (!tbody) return;

  if (sensorData.length === 0) {
    tbody.innerHTML = '<tr><td colspan="10" style="text-align:center;color:var(--text-muted);">No data</td></tr>';
    return;
  }

  tbody.innerHTML = sensorData.map(d => {
    const patrol = formatTaskId(d.task_id);
    const time = d.timestamp ? new Date(d.timestamp).toLocaleString() : '--';
    const validClass = d.is_valid ? 'status-valid' : 'status-invalid';
    return `<tr>
      <td>${patrol}</td>
      <td>${time}</td>
      <td>${d.bed_name || '--'}</td>
      <td>${d.location_id || '--'}</td>
      <td>${d.retry_count ?? '--'}</td>
      <td>${d.status ?? '--'}</td>
      <td>${d.bpm ?? '--'}</td>
      <td>${d.rpm ?? '--'}</td>
      <td class="${validClass}">${d.is_valid ? 'Valid' : 'Invalid'}</td>
      <td>${d.details || '--'}</td>
    </tr>`;
  }).join('');
}

function exportSensorCSV() {
  if (sensorData.length === 0) {
    alert('No data to export');
    return;
  }

  const headers = ['task_id', 'timestamp', 'bed_name', 'location_id', 'retry_count', 'status', 'bpm', 'rpm', 'is_valid', 'details'];
  const rows = sensorData.map(d =>
    headers.map(h => JSON.stringify(d[h] ?? '')).join(',')
  );
  const csv = [headers.join(','), ...rows].join('\n');

  const blob = new Blob([csv], { type: 'text/csv' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = `sensor_data_${new Date().toISOString().slice(0, 10)}.csv`;
  a.click();
  URL.revokeObjectURL(url);
}

// ═══════════════════════════════════════════════════════════════════════════
// SETTINGS TAB
// ═══════════════════════════════════════════════════════════════════════════

const SETTINGS_MAP = [
  { id: 'setting-shelf-id', key: 'shelf_id' },
  { id: 'setting-robot-ip', key: 'robot_ip' },
  { id: 'setting-mqtt-broker', key: 'mqtt_broker' },
  { id: 'setting-mqtt-port', key: 'mqtt_port', type: 'number' },
  { id: 'setting-mqtt-topic', key: 'mqtt_topic' },
  { id: 'setting-mqtt-enabled', key: 'mqtt_enabled', type: 'checkbox' },
  { id: 'setting-bio-scan-wait-time', key: 'bio_scan_wait_time', type: 'number' },
  { id: 'setting-bio-scan-retry-count', key: 'bio_scan_retry_count', type: 'number' },
  { id: 'setting-bio-scan-initial-wait', key: 'bio_scan_initial_wait', type: 'number' },
  { id: 'setting-bio-scan-valid-status', key: 'bio_scan_valid_status', type: 'number' },
  { id: 'setting-robot-max-retries', key: 'robot_max_retries', type: 'number' },
  { id: 'setting-robot-retry-base-delay', key: 'robot_retry_base_delay', type: 'number' },
  { id: 'setting-robot-retry-max-delay', key: 'robot_retry_max_delay', type: 'number' },
  { id: 'setting-enable-telegram', key: 'enable_telegram', type: 'checkbox' },
  { id: 'setting-telegram-bot-token', key: 'telegram_bot_token' },
  { id: 'setting-telegram-user-id', key: 'telegram_user_id' },
  { id: 'setting-enable-mqtt-egress', key: 'enable_mqtt_egress', type: 'checkbox' },
  { id: 'setting-mqtt-egress-topic-prefix', key: 'mqtt_egress_topic_prefix' },
  { id: 'setting-gemini-api-key', key: 'gemini_api_key' },
  { id: 'setting-timezone', key: 'timezone' },
  { id: 'setting-bed-card-stale-hours', key: 'bed_card_stale_hours', type: 'number' },
];

async function fetchShelves() {
  const btn = document.getElementById('btn-fetch-shelves');
  const select = document.getElementById('shelf-select');
  const original = btn.textContent;
  btn.textContent = 'Loading...';
  btn.disabled = true;

  try {
    const data = await dataService.getRobotShelves();
    const shelves = Array.isArray(data) ? data : (data?.shelves || []);
    if (!shelves.length) {
      alert('No shelves registered on the robot.');
      return;
    }
    select.innerHTML = '<option value="">-- Select a shelf --</option>';
    shelves.forEach(s => {
      const id = s.id || s.shelf_id || '';
      const name = s.name || id;
      const opt = document.createElement('option');
      opt.value = id;
      opt.textContent = `${name} (${id})`;
      select.appendChild(opt);
    });

    // Pre-select current value
    const current = document.getElementById('setting-shelf-id').value;
    if (current) select.value = current;

    select.style.display = '';
  } catch (e) {
    const detail = e?.response?.data?.detail || e.message;
    alert('Failed to fetch shelves: ' + detail);
  } finally {
    btn.textContent = original;
    btn.disabled = false;
  }
}

async function reconnectRobot() {
  const btn = document.getElementById('btn-reconnect-robot');
  const status = document.getElementById('reconnect-status');
  const original = btn.textContent;
  btn.textContent = 'Reconnecting...';
  btn.disabled = true;
  status.textContent = '';
  status.className = 'reconnect-status';

  try {
    const res = await dataService.reconnectRobot();
    const ok = res.status === 'ok';
    const result = res.result || {};
    if (ok) {
      const serial = result.serial ? ` — serial ${result.serial}` : '';
      status.textContent = `✓ Connected at ${res.ip}${serial}`;
      status.classList.add('reconnect-status-ok');
    } else {
      status.textContent = `✗ Failed at ${res.ip}: ${result.error || 'unknown error'}`;
      status.classList.add('reconnect-status-fail');
    }
  } catch (e) {
    const detail = e?.response?.data?.detail || e.message;
    status.textContent = `✗ Error: ${detail}`;
    status.classList.add('reconnect-status-fail');
  } finally {
    btn.textContent = original;
    btn.disabled = false;
  }
}

function applyShelfSelection() {
  const select = document.getElementById('shelf-select');
  if (select.value) {
    document.getElementById('setting-shelf-id').value = select.value;
  }
}

function switchSettingsSubTab(name) {
  document.querySelectorAll('.settings-subtab-btn').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.settingsSubtab === name);
  });
  document.querySelectorAll('.settings-subtab-content').forEach(panel => {
    panel.classList.toggle('active', panel.dataset.settingsSubtabContent === name);
  });
}

async function loadSettings() {
  try {
    const settings = await dataService.getSettings();
    SETTINGS_MAP.forEach(({ id, key, type }) => {
      const el = document.getElementById(id);
      if (!el) return;
      if (type === 'checkbox') {
        el.checked = !!settings[key];
      } else {
        el.value = settings[key] ?? '';
      }
    });
  } catch (e) {
    console.error('Failed to load settings:', e);
  }
  loadMapList();
  if (typeof window.loadButtons === 'function') {
    window.loadButtons();
  }
}

async function saveSettings() {
  const data = {};
  SETTINGS_MAP.forEach(({ id, key, type }) => {
    const el = document.getElementById(id);
    if (!el) return;
    if (type === 'checkbox') {
      data[key] = el.checked;
    } else if (type === 'number') {
      data[key] = parseFloat(el.value) || 0;
    } else {
      data[key] = el.value;
    }
  });

  try {
    await dataService.saveSettings(data);
    if (window.bedGrid?.refreshConfig) bedGrid.refreshConfig();
    alert('Settings saved!');
  } catch (e) {
    alert('Failed to save settings: ' + e.message);
  }
}

// ═══════════════════════════════════════════════════════════════════════════
// SSE LOG STREAMING HELPERS
// ═══════════════════════════════════════════════════════════════════════════

function streamSSE(url, logElId, btnElId) {
  const logEl = document.getElementById(logElId);
  const btnEl = document.getElementById(btnElId);
  if (!logEl) return;

  logEl.textContent = '';
  logEl.classList.add('visible');
  if (btnEl) btnEl.disabled = true;

  const evtSource = new EventSource(url);

  evtSource.onmessage = (event) => {
    try {
      const data = JSON.parse(event.data);
      const line = document.createElement('div');
      line.textContent = data.msg;
      if (data.level && data.level !== 'info') {
        line.className = `log-${data.level}`;
      }
      logEl.appendChild(line);
      logEl.scrollTop = logEl.scrollHeight;

      if (data.level === 'done') {
        evtSource.close();
        if (btnEl) btnEl.disabled = false;
      }
    } catch (e) {
      const line = document.createElement('div');
      line.textContent = event.data;
      logEl.appendChild(line);
      logEl.scrollTop = logEl.scrollHeight;
    }
  };

  evtSource.onerror = () => {
    evtSource.close();
    if (btnEl) btnEl.disabled = false;
    const line = document.createElement('div');
    line.textContent = 'Stream closed.';
    line.className = 'log-done';
    logEl.appendChild(line);
  };
}

function testMQTT() {
  streamSSE('/api/settings/test-mqtt', 'mqtt-test-log', 'btn-test-mqtt');
}

function testBioScan() {
  streamSSE('/api/settings/test-bio-scan', 'bioscan-test-log', 'btn-test-bioscan');
}

// ═══════════════════════════════════════════════════════════════════════════
// MAP MANAGEMENT
// ═══════════════════════════════════════════════════════════════════════════

async function loadMapList() {
  const container = document.getElementById('map-list-container');
  if (!container) return;

  try {
    const res = await dataService.getMapList();
    const maps = res.maps || [];
    const activeMap = res.active_map || '';

    if (maps.length === 0) {
      container.innerHTML = '<p style="color:var(--text-muted);font-size:12px;">No saved maps</p>';
      return;
    }

    container.innerHTML = maps.map(m => {
      const isActive = m.id === activeMap;
      const ts = m.timestamp ? new Date(m.timestamp).toLocaleString() : '';
      return `<div class="map-list-item ${isActive ? 'active-map' : ''}">
        <div class="map-info">
          <div class="map-name">${m.name || m.id} ${isActive ? '(Active)' : ''}</div>
          <div class="map-meta">${m.width}x${m.height} | res=${m.resolution} | ${ts}</div>
        </div>
        <button class="btn-secondary" onclick="useMap('${m.id}')" ${isActive ? 'disabled' : ''}>
          ${isActive ? 'Active' : 'Use'}
        </button>
      </div>`;
    }).join('');
  } catch (e) {
    container.innerHTML = '<p style="color:var(--text-muted);font-size:12px;">Failed to load maps</p>';
  }
}

async function fetchMapFromRobot() {
  const btn = document.getElementById('btn-fetch-map');
  if (btn) {
    btn.disabled = true;
    btn.textContent = 'Fetching...';
  }

  try {
    const res = await dataService.fetchMapFromRobot();
    const count = res.maps?.length || 0;
    await loadMapList();
    if (count > 0) {
      alert(`Fetched ${count} map(s) from robot`);
    } else {
      alert('No maps found on robot');
    }
  } catch (e) {
    alert('Failed to fetch maps: ' + (e.response?.data?.detail || e.message || e));
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.textContent = 'Fetch from Robot';
    }
  }
}

async function useMap(mapId) {
  try {
    await dataService.switchMap(mapId);
    await loadMapList();
  } catch (e) {
    alert('Failed to switch map: ' + (e.response?.data?.detail || e.message || e));
  }
}
