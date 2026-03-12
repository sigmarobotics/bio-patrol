# Bio Patrol — Architecture

## Overview

Bio Patrol is an autonomous hospital ward patrol system built on the **Kachaka mobile robot** platform. The robot carries a sensor-equipped shelf to each bed, collects physiological data (heart rate, respiration rate) from **WiSleep bio-sensors** via MQTT, stores readings in SQLite, and sends Telegram alerts when abnormalities are detected.

The system uses **FastAPI** with an async task execution engine. Robot control flows through [`kachaka-sdk-toolkit`](https://github.com/sigmarobotics/kachaka-sdk-toolkit) (`kachaka_core`) with connection pooling, `@with_retry` decorators, and `RobotController` for command-ID verified execution.

## System Architecture

```mermaid
graph TB
    subgraph Browser["Browser (http://localhost:8000)"]
        UI["SPA Dashboard<br/>5 tabs"]
    end

    subgraph Backend["FastAPI Backend"]
        API["REST API<br/>Routers: tasks, kachaka,<br/>settings, bio_sensor"]
        TaskEngine["TaskEngine<br/>Sequential step execution<br/>+ shelf monitor"]
        Scheduler["APScheduler<br/>Cron-style triggers"]
        FleetAPI["FleetAPI<br/>Async bridge to kachaka_core"]
    end

    subgraph Services["Background Services"]
        MQTT["BioSensorMQTTClient<br/>paho-mqtt subscriber"]
        TG["TelegramService<br/>httpx async"]
    end

    subgraph Data["Data Layer"]
        SQLite[(SQLite<br/>sensor_data.db)]
        Config["JSON Config<br/>data/config/"]
    end

    subgraph External["External Systems"]
        Robot["Kachaka Robot<br/>gRPC (kachaka_core)"]
        Sensor["WiSleep Sensor<br/>MQTT Broker"]
        TGBot["Telegram Bot API"]
    end

    UI -->|"HTTP"| API
    Scheduler -->|"trigger"| API
    API --> TaskEngine
    TaskEngine -->|"asyncio.to_thread"| FleetAPI --> Robot
    MQTT -->|"subscribe"| Sensor
    TaskEngine -->|"poll"| MQTT
    TaskEngine --> SQLite
    TaskEngine --> TG --> TGBot
    API --> Config
    API --> SQLite
```

## Patrol Execution Flow

```mermaid
sequenceDiagram
    participant User
    participant API as FastAPI
    participant Engine as TaskEngine
    participant Fleet as FleetAPI
    participant Robot as Kachaka Robot
    participant MQTT as MQTT Client
    participant Sensor as WiSleep
    participant DB as SQLite
    participant TG as Telegram

    User->>API: POST /api/patrol/start
    API->>Engine: Build & submit task

    loop Each Bed
        Engine->>Fleet: move_shelf(shelf, bed)
        Fleet->>Robot: RobotController.move_shelf()
        Robot-->>Fleet: OK (command_id verified)
        Engine->>Engine: Start shelf monitor (3s poll)

        Engine->>MQTT: Poll latest_data
        Note over Engine,MQTT: 120s initial wait<br/>then 19 retries @ 10s
        Sensor-->>MQTT: {bpm, rpm, status}
        MQTT-->>Engine: Valid scan data
        Engine->>DB: Save sensor_scan_data

        alt Shelf dropped
            Engine->>DB: Record remaining beds as SKIPPED
            Engine->>TG: Shelf drop alert
            Engine->>Fleet: return_home()
        end
    end

    Engine->>Fleet: return_shelf()
    Engine->>TG: Patrol complete
```

## Task Engine

The `TaskEngine` is the core execution component. It processes tasks as ordered sequences of steps with conditional skip logic.

### Step Types

| Step | Action | Description |
|------|--------|-------------|
| `move_shelf` | Move shelf to bed location | Starts shelf monitor on success |
| `bio_scan` | Read sensor data via MQTT | 120s wait + 19 retries @ 10s |
| `return_shelf` | Return shelf to home | Stops shelf monitor first |
| `wait` | Delay between steps | Configurable duration |
| `speak` | Robot speech | Announcement at bed |
| `return_home` | Return to charger | End of patrol |

### Conditional Skip Logic

```mermaid
graph LR
    M["move_shelf<br/>(move to bed)"] -->|"success"| S["bio_scan<br/>(read sensor)"]
    M -->|"failure"| SKIP["SKIP bio_scan<br/>Record reason to DB"]
    SKIP --> NEXT["Next bed"]
    S --> NEXT
```

If `move_shelf` fails, linked `bio_scan` steps are skipped (recorded in DB with reason). The patrol continues to the next bed.

### Shelf Drop Detection

```mermaid
stateDiagram-v2
    [*] --> Monitoring: move_shelf success
    Monitoring --> Normal: get_moving_shelf() returns shelf_id
    Normal --> Monitoring: 3s interval
    Monitoring --> Dropped: shelf_id becomes None
    Dropped --> RecordSkipped: Record remaining beds
    RecordSkipped --> AlertTelegram: Send notification
    AlertTelegram --> ReturnHome: Robot goes to charger
    ReturnHome --> ShelfDropped: Task status
    ShelfDropped --> Recover: POST /patrol/recover-shelf
    Recover --> Resume: POST /patrol/resume
    Resume --> [*]: New task with remaining beds
```

## Kachaka Integration (kachaka_core)

All robot operations flow through `kachaka_core` via the `FleetAPI` async bridge:

```mermaid
graph LR
    subgraph FastAPI["FastAPI (async)"]
        Handler["Route handler"]
    end

    subgraph Bridge["FleetAPI"]
        Async["async method"]
        Thread["asyncio.to_thread()"]
    end

    subgraph Core["kachaka_core (sync)"]
        Conn["KachakaConnection<br/>Pooled, thread-safe"]
        Ctrl["RobotController<br/>command_id tracking"]
        Cmds["KachakaCommands<br/>@with_retry"]
        Queries["KachakaQueries<br/>@with_retry"]
    end

    subgraph Robot["Kachaka Robot"]
        gRPC["gRPC Server<br/>:26400"]
    end

    Handler --> Async --> Thread --> Ctrl --> gRPC
    Thread --> Cmds --> gRPC
    Thread --> Queries --> gRPC
    Conn --> gRPC
```

### Per-Robot Slot

Each registered robot gets a `_RobotSlot` containing:
- `KachakaConnection` — pooled gRPC connection
- `RobotController` — background state polling + command execution
- `KachakaCommands` — simple operations with retry
- `KachakaQueries` — status queries with retry

## MQTT Bio-Sensor Flow

```mermaid
sequenceDiagram
    participant Sensor as WiSleep Sensor
    participant Broker as MQTT Broker
    participant Client as BioSensorMQTTClient
    participant Engine as TaskEngine
    participant DB as SQLite

    Sensor->>Broker: Publish {records: [{bpm, rpm, status, ...}]}
    Broker->>Client: Deliver to subscribed topic
    Client->>Client: Update latest_data (in-memory)

    Note over Engine: During bio_scan step
    Engine->>Engine: Wait 120s (sensor settling)
    loop Up to 19 retries
        Engine->>Client: Read latest_data
        alt Valid (status==4, bpm>0, rpm>0)
            Engine->>DB: Save valid scan
        else Invalid
            Engine->>Engine: Wait 10s, retry
        end
    end
```

### Sensor Data Schema

| Field | Type | Description |
|-------|------|-------------|
| `status` | int | 4 = valid measurement |
| `bpm` | float | Heart rate |
| `rpm` | float | Respiration rate |
| `sn` | str | Sensor serial number |
| `signal` | str | Signal quality (e.g., "64/100") |
| `quality` | str | Data quality (e.g., "99/100") |
| `ssid` | str | Bed identifier (e.g., "B03-1") |

## Database

SQLite file: `data/sensor_data.db`

### sensor_scan_data Table

| Column | Type | Description |
|--------|------|-------------|
| `id` | INTEGER PK | Auto-increment |
| `task_id` | TEXT | Links to task |
| `location_id` | TEXT | Robot target location |
| `bed_name` | TEXT | Bed identifier (e.g., 101-1) |
| `timestamp` | TEXT | ISO 8601 |
| `retry_count` | INTEGER | Retries before reading |
| `status` | INTEGER | Sensor status code |
| `bpm` | REAL | Heart rate (NULL if invalid) |
| `rpm` | REAL | Respiration rate (NULL if invalid) |
| `is_valid` | BOOLEAN | Valid reading flag |
| `data_json` | TEXT | Full MQTT record |
| `details` | TEXT | Human-readable notes |

## Configuration

Runtime configs stored as JSON in `data/config/`, merged with defaults on load:

```mermaid
graph LR
    Defaults["defaults.py<br/>Hard-coded defaults"] --> Merge["Merge"]
    Saved["data/config/*.json<br/>User-saved values"] --> Merge
    Merge --> Runtime["Runtime Settings<br/>Fresh per-request"]
```

### Config Files

| File | Purpose |
|------|---------|
| `settings.json` | Robot IP, MQTT, Telegram, scan timing |
| `beds.json` | Room/bed layout and location ID mapping |
| `patrol.json` | Patrol route (bed order, enabled state) |
| `schedule.json` | Scheduled times (daily/weekday) |

### Key Settings

| Setting | Default | Description |
|---------|---------|-------------|
| `robot_ip` | — | Kachaka robot IP:port |
| `mqtt_broker` | `mqtt-broker` | MQTT broker hostname |
| `mqtt_port` | 1883 | MQTT port |
| `bio_scan_initial_wait` | 120 | Initial wait (seconds) |
| `bio_scan_wait_time` | 10 | Retry interval (seconds) |
| `bio_scan_retry_count` | 19 | Max retries |
| `bio_scan_valid_status` | 4 | Required sensor status |
| `shelf_id` | — | Shelf to carry |
| `timezone` | `Asia/Taipei` | Display timezone |

## Frontend

Vanilla JavaScript SPA with Canvas-based map rendering:

| Tab | Features |
|-----|----------|
| Dashboard | Live map, bio-sensor data, schedule, progress bar, quick actions |
| Bed Selection | Click-to-enable grid, auto-save (500ms debounce), presets |
| Location Settings | Room/bed layout, location ID mapping |
| History | Scan history table, statistics cards, CSV export |
| Settings | Robot IP, MQTT, Telegram, scan timing, map management |

## Logging

Per-module rotating log files in `data/logs/`:

| File | Modules |
|------|---------|
| `app.log` | Main lifecycle, fleet, telegram, settings |
| `task.log` | Patrol execution, robot commands |
| `sensor.log` | MQTT sensor data |
| `scheduler.log` | APScheduler events |

## CI/CD

GitHub Actions builds multi-arch Docker images:

- Platforms: `linux/amd64`, `linux/arm64`
- Registry: `ghcr.io/sigmarobotics/bio-patrol`
- Triggers: push to `main`, version tags
