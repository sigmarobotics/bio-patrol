# Bio Patrol — 系統架構

## 概述

Bio Patrol 是基於 **Kachaka 移動機器人** 的自動化病房巡房系統。機器人攜帶搭載感測器的貨架依序移動至各床位，透過 MQTT 從 **WiSleep 生理感測器** 收集生命徵象（心率、呼吸率），數據存入 SQLite，異常時即時透過 Telegram 通報。

系統使用 **FastAPI** 搭配非同步任務執行引擎。機器人控制透過 [`kachaka-sdk-toolkit`](https://github.com/sigmarobotics/kachaka-sdk-toolkit)（`kachaka_core`）實現連線池管理、`@with_retry` 重試裝飾器，以及 `RobotController` 的 command_id 驗證執行。

## 系統架構圖

```mermaid
graph TB
    subgraph Browser["瀏覽器 (http://localhost:8000)"]
        UI["SPA 儀表板<br/>5 個分頁"]
    end

    subgraph Backend["FastAPI 後端"]
        API["REST API<br/>路由：tasks, kachaka,<br/>settings, bio_sensor"]
        TaskEngine["TaskEngine<br/>循序步驟執行<br/>+ 貨架監控"]
        Scheduler["APScheduler<br/>排程觸發"]
        FleetAPI["FleetAPI<br/>kachaka_core 非同步橋接"]
    end

    subgraph Services["背景服務"]
        MQTT["BioSensorMQTTClient<br/>paho-mqtt 訂閱"]
        TG["TelegramService<br/>httpx 非同步"]
    end

    subgraph Data["資料層"]
        SQLite[(SQLite<br/>sensor_data.db)]
        Config["JSON 設定<br/>data/config/"]
    end

    subgraph External["外部系統"]
        Robot["Kachaka 機器人<br/>gRPC (kachaka_core)"]
        Sensor["WiSleep 感測器<br/>MQTT Broker"]
        TGBot["Telegram Bot API"]
    end

    UI -->|"HTTP"| API
    Scheduler -->|"觸發"| API
    API --> TaskEngine
    TaskEngine -->|"asyncio.to_thread"| FleetAPI --> Robot
    MQTT -->|"訂閱"| Sensor
    TaskEngine -->|"輪詢"| MQTT
    TaskEngine --> SQLite
    TaskEngine --> TG --> TGBot
    API --> Config
    API --> SQLite
```

## 巡房執行流程

```mermaid
sequenceDiagram
    participant User as 使用者
    participant API as FastAPI
    participant Engine as TaskEngine
    participant Fleet as FleetAPI
    participant Robot as Kachaka 機器人
    participant MQTT as MQTT Client
    participant Sensor as WiSleep
    participant DB as SQLite
    participant TG as Telegram

    User->>API: POST /api/patrol/start
    API->>Engine: 建立並提交任務

    loop 每個床位
        Engine->>Fleet: move_shelf(貨架, 床位)
        Fleet->>Robot: RobotController.move_shelf()
        Robot-->>Fleet: OK (command_id 驗證)
        Engine->>Engine: 啟動貨架監控 (3秒輪詢)

        Engine->>MQTT: 輪詢 latest_data
        Note over Engine,MQTT: 初始等待 120 秒<br/>之後每 10 秒重試，最多 19 次
        Sensor-->>MQTT: {bpm, rpm, status}
        MQTT-->>Engine: 有效掃描數據
        Engine->>DB: 儲存 sensor_scan_data

        alt 貨架掉落
            Engine->>DB: 記錄剩餘床位為 SKIPPED
            Engine->>TG: 貨架掉落警報
            Engine->>Fleet: return_home()
        end
    end

    Engine->>Fleet: return_shelf()
    Engine->>TG: 巡房完成通知
```

## 任務引擎

`TaskEngine` 是核心執行元件，將任務拆解為有序步驟序列，並支援條件式跳過邏輯。

### 步驟類型

| 步驟 | 動作 | 說明 |
|------|------|------|
| `move_shelf` | 搬運貨架至床位 | 成功後啟動貨架監控 |
| `bio_scan` | 透過 MQTT 讀取感測器 | 120 秒等待 + 19 次重試 @ 10 秒 |
| `return_shelf` | 歸還貨架 | 先停止貨架監控 |
| `wait` | 步驟間延遲 | 可配置時間 |
| `speak` | 機器人語音 | 床邊播報 |
| `return_home` | 返回充電座 | 巡房結束 |

### 條件式跳過邏輯

```mermaid
graph LR
    M["move_shelf<br/>(移動至床位)"] -->|"成功"| S["bio_scan<br/>(讀取感測器)"]
    M -->|"失敗"| SKIP["跳過 bio_scan<br/>記錄原因至 DB"]
    SKIP --> NEXT["下一床位"]
    S --> NEXT
```

若 `move_shelf` 失敗，關聯的 `bio_scan` 步驟被跳過（原因記錄至 DB）。巡房繼續至下一床位。

### 貨架掉落偵測與恢復

```mermaid
stateDiagram-v2
    [*] --> 監控中: move_shelf 成功
    監控中 --> 正常: get_moving_shelf() 回傳 shelf_id
    正常 --> 監控中: 3 秒間隔
    監控中 --> 掉落: shelf_id 變為 None
    掉落 --> 記錄跳過: 記錄剩餘床位
    記錄跳過 --> 通知: Telegram 警報
    通知 --> 返回充電座: 機器人返回
    返回充電座 --> SHELF_DROPPED: 任務狀態
    SHELF_DROPPED --> 歸位: POST /patrol/recover-shelf
    歸位 --> 繼續巡房: POST /patrol/resume
    繼續巡房 --> [*]: 建立新任務（剩餘床位）
```

## Kachaka 整合 (kachaka_core)

所有機器人操作透過 `FleetAPI` 非同步橋接至 `kachaka_core`：

```mermaid
graph LR
    subgraph FastAPI["FastAPI (非同步)"]
        Handler["路由處理器"]
    end

    subgraph Bridge["FleetAPI"]
        Async["async 方法"]
        Thread["asyncio.to_thread()"]
    end

    subgraph Core["kachaka_core (同步)"]
        Conn["KachakaConnection<br/>連線池，執行緒安全"]
        Ctrl["RobotController<br/>command_id 追蹤"]
        Cmds["KachakaCommands<br/>@with_retry"]
        Queries["KachakaQueries<br/>@with_retry"]
    end

    subgraph Robot["Kachaka 機器人"]
        gRPC["gRPC Server<br/>:26400"]
    end

    Handler --> Async --> Thread --> Ctrl --> gRPC
    Thread --> Cmds --> gRPC
    Thread --> Queries --> gRPC
    Conn --> gRPC
```

### 每機器人 Slot

每台註冊的機器人包含一組 `_RobotSlot`：
- `KachakaConnection` — 池化 gRPC 連線
- `RobotController` — 背景狀態輪詢 + 命令執行
- `KachakaCommands` — 簡單操作含重試
- `KachakaQueries` — 狀態查詢含重試

## MQTT 生理感測器流程

```mermaid
sequenceDiagram
    participant Sensor as WiSleep 感測器
    participant Broker as MQTT Broker
    participant Client as BioSensorMQTTClient
    participant Engine as TaskEngine
    participant DB as SQLite

    Sensor->>Broker: 發布 {records: [{bpm, rpm, status, ...}]}
    Broker->>Client: 遞送至訂閱主題
    Client->>Client: 更新 latest_data（記憶體）

    Note over Engine: bio_scan 步驟執行中
    Engine->>Engine: 等待 120 秒（感測器穩定）
    loop 最多 19 次重試
        Engine->>Client: 讀取 latest_data
        alt 有效 (status==4, bpm>0, rpm>0)
            Engine->>DB: 儲存有效掃描
        else 無效
            Engine->>Engine: 等待 10 秒，重試
        end
    end
```

## 資料庫

SQLite 檔案：`data/sensor_data.db`

### sensor_scan_data 資料表

| 欄位 | 類型 | 說明 |
|------|------|------|
| `id` | INTEGER PK | 自動遞增 |
| `task_id` | TEXT | 關聯任務 |
| `location_id` | TEXT | 機器人目標位置 |
| `bed_name` | TEXT | 床位名稱（如 101-1） |
| `timestamp` | TEXT | ISO 8601 格式 |
| `retry_count` | INTEGER | 讀取前重試次數 |
| `status` | INTEGER | 感測器狀態碼 |
| `bpm` | REAL | 心率（無效時 NULL） |
| `rpm` | REAL | 呼吸率（無效時 NULL） |
| `is_valid` | BOOLEAN | 有效讀取標記 |
| `data_json` | TEXT | 完整 MQTT 記錄 |
| `details` | TEXT | 人類可讀備註 |

## 設定系統

執行時設定以 JSON 格式儲存於 `data/config/`，載入時與預設值合併：

```mermaid
graph LR
    Defaults["defaults.py<br/>硬編碼預設值"] --> Merge["合併"]
    Saved["data/config/*.json<br/>使用者儲存值"] --> Merge
    Merge --> Runtime["執行時設定<br/>每次請求重新載入"]
```

### 設定檔案

| 檔案 | 用途 |
|------|------|
| `settings.json` | 機器人 IP、MQTT、Telegram、掃描時間參數 |
| `beds.json` | 病房/床位配置與 location ID 對應 |
| `patrol.json` | 巡房路線（床位順序、啟用狀態） |
| `schedule.json` | 排程時間（每日/工作日） |

### 主要設定項

| 設定 | 預設值 | 說明 |
|------|--------|------|
| `robot_ip` | — | Kachaka 機器人 IP:port |
| `mqtt_broker` | `mqtt-broker` | MQTT broker 主機名稱 |
| `mqtt_port` | 1883 | MQTT 端口 |
| `bio_scan_initial_wait` | 120 | 初始等待秒數 |
| `bio_scan_wait_time` | 10 | 重試間隔秒數 |
| `bio_scan_retry_count` | 19 | 最大重試次數 |
| `bio_scan_valid_status` | 4 | 有效感測器狀態碼 |
| `shelf_id` | — | 搬運貨架 ID |
| `timezone` | `Asia/Taipei` | 顯示時區 |

## 前端

Vanilla JavaScript SPA，使用 Canvas 渲染地圖：

| 分頁 | 功能 |
|------|------|
| Dashboard | 即時地圖、感測器數據、排程、進度條、快速操作 |
| 床位選擇 | 點選啟用格子、自動儲存（500ms debounce）、預設路線 |
| 位置設定 | 病房/床位配置、location ID 對應 |
| 歷史紀錄 | 掃描歷史表格、統計卡片、CSV 匯出 |
| Settings | 機器人 IP、MQTT、Telegram、掃描參數、地圖管理 |

## 日誌系統

每模組獨立的旋轉日誌檔案，位於 `data/logs/`：

| 檔案 | 涵蓋模組 |
|------|---------|
| `app.log` | 主程式生命週期、fleet、telegram、settings |
| `task.log` | 巡房執行、機器人命令 |
| `sensor.log` | MQTT 感測器資料 |
| `scheduler.log` | APScheduler 事件 |

## CI/CD

GitHub Actions 建置多架構 Docker 映像：

- 平台：`linux/amd64`、`linux/arm64`
- Registry：`ghcr.io/sigmarobotics/bio-patrol`
- 觸發：推送至 `main`、版本標籤
