# Bio Patrol Deployment

Production deployment via Docker Compose. Supports x86_64 and ARM64 (Nvidia Jetson, Raspberry Pi).

## Prerequisites

- Docker Engine 24+
- Docker Compose v2

## Quick Start

```bash
cd deploy

# Edit config for your environment
nano data/config/settings.json

# Pull and start
docker compose -f docker-compose.prod.yml pull
docker compose -f docker-compose.prod.yml up -d
```

App runs at `http://<host-ip>:8000`.

## Configuration

All runtime configs are in `deploy/data/config/`. Edit before first start:

| File | What to set |
|------|-------------|
| `settings.json` | `robot_ip` (Kachaka address), `mqtt_broker` (keep `"mqtt-broker"` for bundled Mosquitto), `mqtt_enabled`, Telegram tokens |
| `beds.json` | Room layout — `room_count`, `room_start`, bed definitions |
| `patrol.json` | `shelf_id` and `beds_order` (patrol route) |
| `schedule.json` | Scheduled patrol times (`daily` / `weekday`) |

### Connecting to Kachaka

Set `robot_ip` in `settings.json` to your robot's gRPC address:

```json
{
  "robot_ip": "192.168.1.100:26400"
}
```

### Enabling Bio-sensor (MQTT)

The bundled Mosquitto broker runs on port 1883. Set in `settings.json`:

```json
{
  "mqtt_enabled": true,
  "mqtt_broker": "mqtt-broker",
  "mqtt_port": 1883,
  "mqtt_topic": "/your/sensor/topic"
}
```

### Enabling Telegram Alerts

```json
{
  "enable_telegram": true,
  "telegram_bot_token": "123456:ABC...",
  "telegram_user_id": "987654321"
}
```

### Zigbee Dongle (one-time host setup)

The `zigbee2mqtt` service requires a host-side udev rule that pins the
SONOFF dongle to `/dev/zigbee`. Combined with the compose file's
`privileged: true` + `/dev:/dev` mount, this lets the container
auto-recover from a dongle unplug+replug without operator intervention
(see CORNER-008).

The full setup procedure — including which udev rule to install for
each dongle revision (CP210x `10c4:ea60` vs CH340 `1a86:55d4`) and the
security trade-off of running z2m privileged — is documented in:

→ [docs/buttons-manual.md §2.1–2.2](../docs/buttons-manual.md#21-udev-rule-for-devzigbee)

### Zigbee config snapshots (power-loss resilience)

The app snapshots z2m's `configuration.yaml` / `database.db` /
`coordinator_backup.json` into `data/z2m-snapshots/` whenever the device list
changes, and `z2m-restore.sh` — wired into the z2m container's entrypoint —
rolls the latest complete generation back on every container start. Without
it, one hard power cut can zero `configuration.yaml`, regenerating the
network key and forcing every paired button to be re-paired. `z2m-restore.sh`
must sit next to `docker-compose.prod.yml`; status is shown in Settings →
硬體設定 → Zigbee 設定快照.

That card goes red when the app cannot snapshot (missing
`Z2M_DATA_DIR`/`Z2M_SNAPSHOT_DIR`, or the `zigbee2mqtt/` mount not taking
effect — snapshots stop while the restore keeps rolling back, the worst
combination) and when a boot restore did not put every file back. Grey
「未啟用（本機開發）」 means neither env var is set at all, which is only
correct off the Pi — on the Pi it means the compose file is out of date.

## Commands

```bash
# Start in background
docker compose -f docker-compose.prod.yml up -d

# View logs
docker compose -f docker-compose.prod.yml logs -f app

# Stop
docker compose -f docker-compose.prod.yml down

# Update to latest image
docker compose -f docker-compose.prod.yml pull
docker compose -f docker-compose.prod.yml up -d
```

## Data Persistence

| Path | Content |
|------|---------|
| `data/config/` | JSON configs (survives updates) |
| `data/sensor_data.db` | SQLite scan history (created at runtime) |
| `mosquitto-data` | MQTT broker persistence (Docker volume) |

All data is preserved across container restarts and image updates.

## File Structure

```
deploy/
├── docker-compose.prod.yml   # Production compose file
├── mosquitto.conf             # Mosquitto broker config
├── z2m-restore.sh             # Runs inside the z2m container on every start

├── data/
│   └── config/                # Runtime JSON configs
│       ├── settings.json
│       ├── beds.json
│       ├── patrol.json
│       └── schedule.json
└── README.md
```
