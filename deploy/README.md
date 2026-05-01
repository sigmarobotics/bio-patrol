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

The `zigbee2mqtt` service uses a host udev rule to map the SONOFF SNZB
Zigbee dongle to a stable `/dev/zigbee` symlink. Combined with
`privileged: true` + `/dev:/dev` in the compose file, this lets the
container survive a dongle unplug + replug **without operator
intervention** — udev re-creates the symlink and z2m reconnects
automatically (CORNER-008).

Install once on the host:

```bash
sudo tee /etc/udev/rules.d/99-zigbee.rules > /dev/null <<'EOF'
SUBSYSTEM=="tty", ATTRS{idVendor}=="10c4", ATTRS{idProduct}=="ea60", SYMLINK+="zigbee"
EOF
sudo udevadm control --reload-rules
sudo udevadm trigger
```

Verify with the dongle plugged in:

```bash
ls -l /dev/zigbee   # → symlink to /dev/ttyUSB0 or similar
lsusb | grep '10c4:ea60'   # → Silicon Labs CP210x UART Bridge
```

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
├── data/
│   └── config/                # Runtime JSON configs
│       ├── settings.json
│       ├── beds.json
│       ├── patrol.json
│       └── schedule.json
└── README.md
```
