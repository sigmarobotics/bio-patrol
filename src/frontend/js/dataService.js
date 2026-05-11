// Data Service Layer - Kachaka Care Command Center
// Unified interface for all backend API calls

class DataService {
  constructor() {
    this.robotId = 'kachaka';
  }

  // ═══════════════════════════════════════════════════════════════════════════
  // SETTINGS / CONFIG APIs
  // ═══════════════════════════════════════════════════════════════════════════

  async getSettings() {
    const res = await axios.get('/api/settings');
    return res.data;
  }

  async saveSettings(data) {
    const res = await axios.post('/api/settings', data);
    return res.data;
  }

  async reconnectRobot() {
    const res = await axios.post('/api/settings/reconnect-robot');
    return res.data;
  }

  async getBeds() {
    const res = await axios.get('/api/beds');
    return res.data;
  }

  async saveBeds(data) {
    const res = await axios.post('/api/beds', data);
    return res.data;
  }

  async getPatrol() {
    const res = await axios.get('/api/patrol');
    return res.data;
  }

  async savePatrol(data) {
    const res = await axios.post('/api/patrol', data);
    return res.data;
  }

  async getPatrolPresets() {
    const res = await axios.get('/api/patrol/presets');
    return res.data;
  }

  async savePatrolPreset(name) {
    const res = await axios.post(`/api/patrol/presets/${encodeURIComponent(name)}`);
    return res.data;
  }

  async loadPatrolPreset(name) {
    const res = await axios.post(`/api/patrol/presets/${encodeURIComponent(name)}/load`);
    return res.data;
  }

  async deletePatrolPreset(name) {
    const res = await axios.delete(`/api/patrol/presets/${encodeURIComponent(name)}`);
    return res.data;
  }

  async setDemoPreset(name) {
    const res = await axios.post(`/api/patrol/presets/${encodeURIComponent(name)}/set-demo`);
    return res.data;
  }

  async getSchedules() {
    const res = await axios.get('/api/schedule');
    return res.data;
  }

  async saveSchedules(data) {
    const res = await axios.post('/api/schedule', data);
    return res.data;
  }

  async deleteSchedule(scheduleId) {
    const res = await axios.delete(`/api/schedule/${scheduleId}`);
    return res.data;
  }

  // ═══════════════════════════════════════════════════════════════════════════
  // PATROL CONTROL APIs
  // ═══════════════════════════════════════════════════════════════════════════

  async startPatrol(mode = 'patrol') {
    const res = await axios.post('/api/patrol/start', { mode });
    return res.data;
  }

  async recoverShelf(shelfId) {
    const res = await axios.post('/api/patrol/recover-shelf', {
      shelf_id: shelfId
    });
    return res.data;
  }

  async resumePatrol(taskId) {
    const res = await axios.post('/api/patrol/resume', { task_id: taskId });
    return res.data;
  }

  // ═══════════════════════════════════════════════════════════════════════════
  // TASK APIs
  // ═══════════════════════════════════════════════════════════════════════════

  async getTasks() {
    const res = await axios.get('/api/tasks');
    return res.data;
  }

  async getTask(taskId) {
    const res = await axios.get(`/api/tasks/${taskId}`);
    return res.data;
  }

  async createTask(taskData) {
    const res = await axios.post('/api/tasks', taskData);
    return res.data;
  }

  async cancelTask(taskId) {
    const res = await axios.post(`/api/tasks/${taskId}/cancel`);
    return res.data;
  }

  // ═══════════════════════════════════════════════════════════════════════════
  // BIO-SENSOR APIs
  // ═══════════════════════════════════════════════════════════════════════════

  async getLatestByBed() {
    const res = await axios.get('/api/bio-sensor/latest-by-bed');
    return res.data;
  }

  async getScanHistoryByLocation(locationId, limit = 5) {
    const res = await axios.get('/api/bio-sensor/scan-history', {
      params: { location_id: locationId, limit }
    });
    return res.data;
  }

  async getSensorHistory(params = {}) {
    const query = new URLSearchParams(params).toString();
    const res = await axios.get(`/api/bio-sensor/scan-history?${query}`);
    return res.data;
  }

  // ═══════════════════════════════════════════════════════════════════════════
  // ROBOT APIs (hardcoded robot_id = "kachaka")
  // ═══════════════════════════════════════════════════════════════════════════

  async getRobotConnectionState() {
    const res = await axios.get('/api/robot/connection-state');
    return res.data;
  }

  async getRobotLocations() {
    const res = await axios.get(`/kachaka/${this.robotId}/locations`);
    return res.data;
  }

  async getRobotShelves() {
    const res = await axios.get(`/kachaka/${this.robotId}/shelves`);
    return res.data;
  }

  async getRobotBattery() {
    const res = await axios.get(`/kachaka/${this.robotId}/battery`);
    return res.data;
  }

  async getRobotPose() {
    const res = await axios.get(`/kachaka/${this.robotId}/pose`);
    return res.data;
  }

  async getRobotMap() {
    const res = await axios.get(`/kachaka/${this.robotId}/map`);
    return res.data;
  }

  // ═══════════════════════════════════════════════════════════════════════════
  // MAP MANAGEMENT APIs
  // ═══════════════════════════════════════════════════════════════════════════

  async getMapList() {
    const res = await axios.get('/api/maps');
    return res.data;
  }

  async fetchMapFromRobot() {
    const res = await axios.post('/api/maps/fetch');
    return res.data;
  }

  async setActiveMap(mapId) {
    const res = await axios.post('/api/maps/active', { map_id: mapId });
    return res.data;
  }

  async getActiveMapInfo() {
    const res = await axios.get('/api/maps/active-info');
    return res.data;
  }

  async switchMap(mapId) {
    const res = await axios.post('/api/maps/switch', { map_id: mapId });
    return res.data;
  }

  async getRobots() {
    const res = await axios.get('/kachaka/robots');
    return res.data;
  }

  async getCommandState() {
    const res = await axios.get(`/kachaka/${this.robotId}/command/state`);
    return res.data;
  }

  async getLastCommand() {
    const res = await axios.get(`/kachaka/${this.robotId}/command/last`);
    return res.data;
  }

  // ═══════════════════════════════════════════════════════════════════════════
  // ROBOT COMMANDS
  // ═══════════════════════════════════════════════════════════════════════════

  async returnHome() {
    const res = await axios.post(`/kachaka/${this.robotId}/command/return_home`, { cancel_all: true });
    return res.data;
  }

  async speak(text) {
    const res = await axios.post(`/kachaka/${this.robotId}/command/speak`, { text });
    return res.data;
  }

  async moveToLocation(locationId) {
    const res = await axios.post(`/kachaka/${this.robotId}/command/move_to_location`, {
      location_name: locationId,
      cancel_all: true
    });
    return res.data;
  }

  async moveShelf(shelfId, locationId) {
    const res = await axios.post(`/kachaka/${this.robotId}/command/move_shelf`, {
      shelf_id: shelfId,
      location_id: locationId,
      cancel_all: true
    });
    return res.data;
  }

  async returnShelf(shelfId) {
    const res = await axios.post(`/kachaka/${this.robotId}/command/return_shelf`, {
      shelf_id: shelfId,
      cancel_all: true
    });
    return res.data;
  }

  async resetShelfPose(shelfId) {
    const res = await axios.post(`/kachaka/${this.robotId}/command/reset_shelf_pose`, {
      shelf_id: shelfId,
      cancel_all: true
    });
    return res.data;
  }

  async moveToPose(x, y, theta) {
    const res = await axios.post(`/kachaka/${this.robotId}/command/move_to_pose`, {
      x, y, theta, cancel_all: true
    });
    return res.data;
  }
}

// Global instance
const dataService = new DataService();
