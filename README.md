# Bio Patrol

Kachaka 機器人自動巡房系統 — 搭載生理感測器，自動巡視病房床位，透過 MQTT 收集心率/呼吸數據，異常時即時 Telegram 通報。

## 系統架構

```
                          gRPC (protobuf)
  ┌──────────┐      ┌──────────────────┐
  │ Frontend │      │  Kachaka Robot   │
  │  (SPA)   │      │  192.168.x.x    │
  └────┬─────┘      └──────┬───────────┘
       │ HTTP/SSE      gRPC│
       v                   v
  ┌───────────────────────────────────────┐
  │           FastAPI Backend             │
  │                                       │
  │  ┌───────────┐   ┌────────────────┐  │
  │  │ Scheduler │──▶│  Task Engine   │  │
  │  │(APScheduler)  │  (TaskEngine)  │  │
  │  └───────────┘   └───────┬────────┘  │
  │                          │           │
  │  ┌────────────┐   ┌──────┴────────┐  │
  │  │ MQTT Client│   │   Fleet API   │  │
  │  │(paho-mqtt) │   │(RobotManager) │  │
  │  └─────┬──────┘   └──────────────┘  │
  │        │                             │
  │  ┌─────┴──────┐  ┌───────────────┐  │
  │  │   SQLite   │  │   Telegram    │  │
  │  │(scan hist) │  │  (httpx)      │  │
  │  └────────────┘  └───────────────┘  │
  └───────────┬───────────────────────────┘
              │ MQTT
       ┌──────┴──────┐
       │  Bio-sensor │
       │  (WiSleep)  │
       └─────────────┘
```

## 技術棧

| 層級 | 技術 |
|------|------|
| Backend | FastAPI, Python 3.12, uvicorn |
| Robot API | gRPC / protobuf (`kachaka-api`) |
| Bio-sensor | MQTT (`paho-mqtt 2.x`) |
| Scheduling | APScheduler |
| Database | SQLite |
| Frontend | Vanilla JS SPA, Canvas (地圖渲染) |
| Notifications | Telegram Bot API (`httpx`) |
| Deployment | Docker multi-arch (amd64 + arm64) |

## 快速開始

### Docker (推薦)

```bash
docker compose up
```

### 本地開發

```bash
uv sync
PYTHONPATH=src/backend uv run uvicorn main:app --app-dir src/backend --reload
```

服務啟動於 http://localhost:8000

## 前端介面

5 個分頁的 SPA 單頁應用：

| 分頁 | 功能 |
|------|------|
| **Dashboard** | 地圖即時顯示、生理感測器最新數據、排程管理（下次巡房時間）、快速操作（Demo Run / Start Patrol）、巡房進度條 |
| **床位選擇** | 巡房路線編輯（點選床位即時自動儲存）、Demo & Presets 管理 |
| **位置設定** | 病房數量/編號設定、床位與機器人 Location ID 對應 |
| **歷史紀錄** | 感測器掃描歷史查詢、統計卡片、CSV 匯出 |
| **Settings** | Robot IP、MQTT、Telegram、Bio-sensor timing、地圖管理 |

### Dashboard 右側面板

- **Latest Bio-Sensor**：最新心率 / 呼吸率 / 狀態
- **Patrol Schedule**：排程清單 + 下次巡房時間（自動計算 daily/weekday）+ 新增排程
- **Quick Actions**：Demo Run / Start Patrol 按鈕
- **Patrol Progress**：巡房啟動後顯示進度條（已完成/總床位數 + 目前掃描床位）+ 取消按鈕

## 核心功能

### 自動巡房

- 依排程或手動觸發巡房任務
- 機器人攜帶感測器貨架依序移動到各床位
- 每個床位停留讀取生理數據（心率、呼吸率）
- 數據存入 SQLite，異常時 Telegram 通報
- 巡房進度即時顯示在 Dashboard（已完成/總數 + 進度條）

### 床位選擇自動儲存

床位選擇頁面點選床位時自動儲存（500ms debounce），無需手動按 Save。

### 貨架掉落偵測

巡房過程中持續監控貨架搬運狀態：

- **偵測方式**：背景 polling (`get_moving_shelf_id()`) 每 3 秒檢查
- **觸發時**：查詢貨架當前位置 → 記錄座標到 task metadata
- **前端顯示**：彈出警示視窗，內嵌地圖標示掉落位置（紅色標記）
- **恢復選項**：僅歸位感測器 / 歸位並繼續巡房（剩餘床位）
- **Monitor 停止時機**：執行 `return_shelf` 步驟前即停止，之後的步驟不再監控

### 取消巡房

巡房進行中可透過 Dashboard 進度條旁的 ✕ 按鈕取消：

- 前端 POST `/api/tasks/{id}/cancel` 設定狀態為 CANCELLED
- 後端即時送出 `cancel_command` 停止機器人當前動作
- Task Engine 偵測 CANCELLED 狀態後中斷迴圈，不會覆寫為 FAILED
- 若機器人正在搬運貨架 → 自動歸位貨架 + 返回充電座
- Telegram 通知「巡房已取消」
- 前端顯示「Patrol cancelled」後自動隱藏進度條

### 移動錯誤處理

- move_shelf / return_shelf 失敗 → 跳過關聯的 bio_scan 步驟
- 被跳過的 bio_scan 記錄到 DB（status=N/A, 原因=機器人無法移動到床邊）
- gRPC transient errors 自動 exponential backoff 重試

### 地圖系統

- 從機器人抓取地圖（protobuf → PNG + metadata）
- Canvas 渲染：地圖底圖 + 機器人即時位置 + 貨架掉落標記
- 支援 pan/zoom（滑鼠 + 觸控）

## 設定

Runtime 設定檔位於 `data/config/`，啟動時與 defaults 合併：

| 檔案 | 用途 |
|------|------|
| `settings.json` | Robot IP, MQTT broker, Telegram tokens, scan 參數 |
| `beds.json` | 病房 / 床位配置 |
| `patrol.json` | 巡房路線（床位順序、啟用狀態） |
| `schedule.json` | 排程巡房時間 |

預設值定義於 [`src/backend/settings/defaults.py`](src/backend/settings/defaults.py)。

## 專案結構

```
bio-patrol/
├── src/
│   ├── backend/
│   │   ├── main.py                 # App entry, lifespan, router mount
│   │   ├── common_types.py         # Task, Step, Status enums
│   │   ├── dependencies.py         # DI: fleet, bio_sensor client
│   │   ├── routers/
│   │   │   ├── kachaka.py          # Robot control endpoints
│   │   │   ├── tasks.py            # Task CRUD & queue
│   │   │   ├── settings.py         # Config, maps, beds, patrol API
│   │   │   └── bio_sensor.py       # Sensor data & scan history
│   │   ├── services/
│   │   │   ├── fleet_api.py        # Fleet abstraction (multi-robot)
│   │   │   ├── robot_manager.py    # Robot client lifecycle & resolver
│   │   │   ├── task_runtime.py     # Task execution engine
│   │   │   ├── scheduler.py        # APScheduler integration
│   │   │   ├── bio_sensor_mqtt.py  # MQTT client + SQLite
│   │   │   └── telegram_service.py # Telegram notifications
│   │   ├── settings/
│   │   │   ├── config.py           # Config loading & paths
│   │   │   └── defaults.py         # Default values
│   │   └── utils/
│   │       └── json_io.py          # JSON read/write helpers
│   │
│   └── frontend/                   # Vanilla JS SPA
│       ├── index.html
│       ├── css/style.css
│       └── js/
│           ├── script.js           # Main UI + map rendering
│           └── dataService.js      # API client (axios)
│
├── data/                           # Runtime data (Docker volume)
│   ├── config/                     # JSON configs
│   └── maps/                       # Map PNG + metadata
│
├── deploy/                         # Production deployment
│   ├── docker-compose.prod.yml
│   └── README.md
│
├── docs/                           # Documentation
│   ├── task_runtime_flow.md        # Task engine flow diagrams
│   ├── BIO_SENSOR.md              # MQTT sensor integration
│   └── GRPC_ERROR_FIX_SUMMARY.md  # Error handling & retry
│
├── Dockerfile                      # Multi-stage, ARM64-ready
├── docker-compose.yml              # Local development
├── pyproject.toml
└── uv.lock
```

## API 端點

### 設定 & 配置

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET/POST` | `/api/settings` | Runtime 設定 |
| `GET/POST` | `/api/beds` | 床位配置 |
| `GET/POST` | `/api/patrol` | 巡房路線 |
| `GET/POST` | `/api/schedule` | 排程設定 |
| `GET` | `/api/maps` | 地圖列表 |
| `POST` | `/api/maps/fetch` | 從機器人抓取地圖 |

### 巡房 & 任務

| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/api/patrol/start` | 啟動巡房（mode: patrol/demo） |
| `POST` | `/api/patrol/resume` | 恢復中斷的巡房 |
| `POST` | `/api/patrol/recover-shelf` | 歸位貨架 |
| `GET` | `/api/tasks` | 任務列表 |
| `GET` | `/api/tasks/{id}` | 任務詳情 |
| `POST` | `/api/tasks/{id}/cancel` | 取消任務（停止機器人 + 歸位貨架） |
| `DELETE` | `/api/tasks/{id}` | 刪除任務 |

### Presets

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/api/patrol/presets` | Preset 列表 |
| `POST` | `/api/patrol/presets/{name}` | 儲存目前路線為 preset |
| `POST` | `/api/patrol/presets/{name}/load` | 載入 preset |
| `POST` | `/api/patrol/presets/{name}/set-demo` | 設為 Demo 路線 |
| `DELETE` | `/api/patrol/presets/{name}` | 刪除 preset |

### Bio-sensor

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/api/bio-sensor/latest` | 最新感測器讀數 |
| `GET` | `/api/bio-sensor/scan-history` | 歷史掃描紀錄 |

### Robot Control

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/kachaka/robots` | 已註冊機器人列表 |
| `GET` | `/kachaka/{id}/battery` | 電池狀態 |
| `GET` | `/kachaka/{id}/pose` | 機器人位置 |
| `POST` | `/kachaka/{id}/command/move_shelf` | 搬運貨架 |
| `POST` | `/kachaka/{id}/command/return_shelf` | 歸還貨架 |
| `POST` | `/kachaka/{id}/command/return_home` | 返回充電座 |

## 資料庫

感測器掃描紀錄儲存在 SQLite (`data/sensor_data.db`)：

| 欄位 | 說明 |
|------|------|
| `location_id` | 機器人移動目標位置 |
| `bed_name` | 床位名稱（如 101-1） |
| `bpm` | 心率 |
| `rpm` | 呼吸率 |
| `status` | 感測器狀態碼（4 = 有效量測） |
| `is_valid` | 是否為有效數據 |
| `retry_count` | 重試次數 |

## 部署

見 [`deploy/README.md`](deploy/README.md)。

CI/CD 透過 GitHub Actions (`.github/workflows/docker.yml`) 自動建置 multi-arch Docker image：

- Platforms: `linux/amd64`, `linux/arm64`
- Registry: `ghcr.io/sigma-snaken/bio-patrol`

## 詳細文件

| 文件 | 內容 |
|------|------|
| [`docs/BIO_SENSOR.md`](docs/BIO_SENSOR.md) | MQTT 感測器整合說明 |
| [`docs/GRPC_ERROR_FIX_SUMMARY.md`](docs/GRPC_ERROR_FIX_SUMMARY.md) | gRPC 錯誤處理 & 重試邏輯 |

## License

This project is licensed under the Apache License 2.0 - see the [LICENSE](LICENSE) file for details.

Copyright 2026 Sigma Robotics
