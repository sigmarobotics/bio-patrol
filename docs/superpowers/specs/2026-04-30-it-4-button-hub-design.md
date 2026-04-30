# IT-4 — Zigbee Button Bindings (bio-patrol)

**Status:** Approved 2026-04-30. Worktree: `.worktrees/it-4-button-hub`. Branch: `feature/it-4-button-hub`.

## Motivation

Two physical, no-tablet operations needed in the hospital ward:

1. **Shelf-recovery resume.** When the shelf detaches from the robot mid-patrol, a button placed at the shelf's home location lets a nurse push the shelf back into the home dock and press once — bio-patrol resets the shelf pose and resumes the remaining beds.
2. **Demo run.** A second button on a demo bench lets visitors trigger the existing dashboard "Demo Run" action without opening the tablet UI.

Both are **physical proxies for HTTP endpoints that already exist** in bio-patrol. IT-4 is therefore primarily a transport + binding layer.

## Reference

`/home/snaken/CodeBase/sigma-button-controller/` — port the MQTT parser, button manager, and pairing flow; drop everything else (multi-robot, route templates, RTT logger, WiFi agent).

## Hardware

- Existing: Raspberry Pi 5 host (192.168.50.149), `eclipse-mosquitto:2.0` already in `docker-compose{,.prod}.yml` on port 1883.
- New: SONOFF Zigbee 3.0 USB dongle on the Pi (path `/dev/zigbee` via udev rule, configurable via `ZIGBEE_SERIAL_PORT`), SONOFF SNZB-01 buttons (battery-powered, 1× shelf-home + 1× demo-bench, more later).

```
SNZB-01 ──Zigbee──► dongle ──► zigbee2mqtt ──► mqtt-broker ──► bio-patrol app
                                                                        │
                                                              existing /api/patrol/* etc.
```

## UX — single trigger, action-centric

The Settings tab gains one full-width `.glass-panel` titled "Zigbee Buttons", placed below the existing two-column grid. It shows a fixed list of registered actions (six, see below). Each row has one button:

- **Pair** — armed only when row is unpaired. Triggers `permit_join` for 120 s; the next `device_joined` event binds that IEEE to this row.
- **Cancel** — appears during the 120 s window.
- **Unpair** — appears when bound; clears the IEEE and (optionally) tells z2m to forget the device.
- **Test** — fires the action immediately for QA.

Single press only. SNZB-01 also emits `double` and `long`, but bio-patrol ignores them.

## Action set (6)

| Key | Label | Internal call |
|---|---|---|
| `demo_run` | Demo Run | `start_patrol(PatrolStartRequest(mode="demo"))` |
| `shelf_resume` | Resume after shelf drop | new `resume_latest_shelf_drop()` (auto-finds latest task with `metadata.shelf_drop=True`, calls `resume_patrol`) |
| `patrol_start` | Start patrol | `start_patrol(PatrolStartRequest(mode="patrol"))` |
| `patrol_cancel` | Cancel running patrol | look up `current_tasks["kachaka"]`, call `cancel_task` |
| `return_home` | Return home | `fleet.return_home("kachaka")` |
| `speak` | Speak (test) | `fleet.speak("kachaka", text)` |

Adding a new action later is one entry in `services/action_registry.py:DEFAULT_ACTIONS`.

## Schema (one new table in `data/sensor_data.db`)

```sql
CREATE TABLE IF NOT EXISTS button_bindings (
    action_key      TEXT PRIMARY KEY,
    ieee_addr       TEXT,
    friendly_name   TEXT,
    paired_at       TEXT,
    battery         INTEGER,
    last_seen       TEXT,
    last_fired_at   TEXT,
    fire_count      INTEGER NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX idx_button_bindings_ieee
    ON button_bindings(ieee_addr) WHERE ieee_addr IS NOT NULL;
```

One row per registered action; rows seeded on startup. `NULL` `ieee_addr` = "listed in UI, no button paired yet". The unique partial index guarantees one IEEE → one action.

## REST API (5 endpoints)

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/button-bindings` | list all rows + active pairing target |
| POST | `/api/button-bindings/{action_key}/pair` | arm pairing window |
| POST | `/api/button-bindings/{action_key}/pair/cancel` | abort pairing |
| DELETE | `/api/button-bindings/{action_key}?forget_device=true` | clear binding |
| POST | `/api/button-bindings/{action_key}/test` | fire now |

Plus one new internal helper exposed for `shelf_resume`:

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/patrol/resume-latest` | find newest task with `metadata.shelf_drop=True`, call `resume_patrol` |

## Pairing safety

`button_manager` holds a single pairing target with a 120 s monotonic-clock expiry behind an `asyncio.Lock`. Only one pairing target at a time. If the target expires before a `device_joined` arrives, no auto-binding occurs.

## CORNER cases

| ID | Scenario | Plan |
|---|---|---|
| CORNER-005 | `shelf_resume` pressed when no shelf-dropped task exists | endpoint returns 400; manager logs warning |
| CORNER-006 | `demo_run` pressed during another running patrol | `submit_task` is single-queue; new task queues behind |
| CORNER-007 | Repeat press within 2 s | per-IEEE debounce in `button_manager` |
| CORNER-008 | Dongle unplugged → `zigbee2mqtt` restarts forever | bio-patrol app keeps running; UI panel shows "MQTT disconnected" pill |
| CORNER-009 | `mqtt-broker` unreachable on startup | aiomqtt auto-reconnects with exponential backoff |

## Files

**New backend**
- `src/backend/services/zigbee_mqtt.py` — aiomqtt loop + `parse_zigbee_message`
- `src/backend/services/button_manager.py` — pairing target + dispatch
- `src/backend/services/action_registry.py` — 6 actions + handlers
- `src/backend/services/button_db.py` — sync sqlite helpers
- `src/backend/routers/buttons.py` — 5 REST endpoints

**Edited backend**
- `src/backend/main.py` — wire startup/shutdown
- `src/backend/routers/settings.py` — add `resume_latest_shelf_drop` + `/api/patrol/resume-latest`
- `src/backend/settings/defaults.py` — `zigbee_mqtt_host`, `zigbee_mqtt_port`, `zigbee_enabled`
- `pyproject.toml` — add `aiomqtt`

**Frontend**
- `src/frontend/index.html` — Zigbee Buttons panel + `<script src="js/buttons.js">`
- `src/frontend/js/buttons.js` — render + Pair/Cancel/Unpair/Test
- `src/frontend/js/script.js` — call `loadButtons()` from `loadSettings()`
- `src/frontend/css/style.css` — minor styling

**Compose / config**
- `docker-compose.yml` + `deploy/docker-compose.prod.yml` — add `zigbee2mqtt` service
- `zigbee2mqtt/configuration.yaml` — z2m config

**Tests**
- `tests/unit/test_zigbee_mqtt_parser.py` — message decoding
- `tests/unit/test_action_registry.py` — registry + dispatch
- `tests/unit/test_button_manager.py` — pairing target + debounce + dispatch
- `tests/unit/test_button_db.py` — schema + bind/unbind/update

## Deployment

CI builds multi-arch (amd64+arm64) image, pushed to GHCR. Deploy by SSH-ing to `sigma@192.168.50.149`, pulling the new compose, restarting. udev rule for the Zigbee dongle must be set up once on the Pi.
