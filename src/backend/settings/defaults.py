"""
Default settings for the Bio Patrol system.
"""

DEFAULT_SETTINGS = {
    "robot_ip": "192.168.204.37:26400",
    "mqtt_broker": "demo.wisleep-eck.org",
    "mqtt_port": 8883,
    "mqtt_topic": "deviceData-qt/201906078",
    "mqtt_username": "",
    "mqtt_password": "",
    "mqtt_tls_cert": "wisleep-key/sigmabot.crt",
    "mqtt_tls_key": "wisleep-key/sigmabot.key",
    "mqtt_shelf_id": "",
    "mqtt_enabled": False,
    "bio_scan_wait_time": 10,
    "bio_scan_retry_count": 19,
    # IT-20: bio_scan_initial_wait 是量測開始前的**無條件** sleep —— 資料早到也
    # 不會提前結束。單床上限 = initial_wait + wait_time × (retry_count − 1)，所以
    # 調小 initial_wait 必須同步調大 retry_count，否則失敗判定窗跟著縮短，會把
    # 收斂較慢的床誤判成失敗。感測器取 1 分鐘移動平均、加上停車後感測桿晃動，
    # 60 秒是物理下限。另外 task_runtime 的低電量中止只採信靜止取樣，而電量是
    # 每 ~30s 輪詢一次；initial_wait 遠小於輪詢週期時，可用的取樣窗會變稀疏，
    # 中止機制會變鈍（見 BATTERY_ABORT_CONSECUTIVE）。
    "bio_scan_initial_wait": 120,
    "bio_scan_valid_status": 4,
    # IT-23: 感測器活性閘門。判斷依據是「感測墊訊息有沒有到達」（墊子無人也約
    # 每 2.5 秒發一筆），不是 MQTT broker 連線狀態——墊子沒電時連線照樣是通的。
    # 啟動前等 wait_seconds 秒內要有訊息；排程被拒啟後每 retry_minutes 分鐘重試、
    # 最多 retry_count 次；巡房中連續 silent_abort_beds 床整窗無訊息即收工。0＝停用。
    "sensor_precheck_wait_seconds": 30,
    "sensor_precheck_retry_count": 3,
    "sensor_precheck_retry_minutes": 10,
    "sensor_silent_abort_beds": 3,
    "arrival_voice_enabled": False,
    "robot_max_retries": 3,
    "robot_retry_base_delay": 2.0,
    "robot_retry_max_delay": 10.0,
    "enable_telegram": False,
    "telegram_bot_token": "",
    "telegram_user_id": "",
    # 通知改走雲端 hub relay（雲端 hub /api/notify）；兩值皆設時 Pi 不再直連
    # Telegram API，bot token 由 hub 持有。
    "notify_hub_url": "",
    "notify_hub_token": "",
    # IT-12: LINE push 通報。line_group_ids 是 webhook 服務捕捉到的 group/user id 清單
    # （由 Settings UI 勾選）；line_webhook_url/api_key 指向 GCP xinyin7f 的 Cloud Run 服務。
    "enable_line": False,
    "line_channel_access_token": "",
    "line_group_ids": [],
    "line_webhook_url": "",
    "line_webhook_api_key": "",
    "gemini_api_key": "",
    "active_map": "",
    "shelf_id": "S_04",
    "demo_preset": "",
    "timezone": "Asia/Taipei",
    "zigbee_enabled": True,
    "zigbee_mqtt_host": "mqtt-broker",
    "zigbee_mqtt_port": 1883,
    "enable_mqtt_egress": False,
    "mqtt_egress_topic_prefix": "bio-patrol/anomaly",
    # IT-9: Bed-card 進入 Stale 態的時數門檻
    "bed_card_stale_hours": 24,
    # IT-10: 機器人連續斷線多久才推 OFFLINE 通知（秒）。預設 5 min — mesh 漫遊瞬斷不算事故。
    "robot_offline_debounce_seconds": 300,
    # TODO-018: 低於此電量（%）不讓巡邏起跑；查詢不到電量時 fail-open 放行。
    "patrol_min_battery_pct": 30,
    # IT-17: 巡房中低於此電量（%）自動取消收工（貨架歸位＋回充）；0 = 停用。
    "patrol_abort_battery_pct": 10,
    # Demo run 最後一床的停留秒數（其餘床固定 5s）。現場展示要機器人停在
    # 終點讓來賓看量測，時間到才自動歸還棚車回充。
    "demo_final_wait_seconds": 5,
    # IT-21: demo run 每座位「量測中」停留秒數（仍不短於到床語音長度）。
    "demo_dwell_seconds": 15,
    # IT-22: demo run 是否跑合成量測（正常→異常→人不在，新民開）。關＝只走不量：
    # 每床停 5 秒、最後一床停 demo_final_wait_seconds，不產任何量測資料與通報。
    "demo_synthetic": False,
    # IT-21: 心跳呼吸閾值警示。正式巡房只在開關打開時評估（新民開、其他站點維持關）；
    # demo 一律評估。閾值為開區間外才觸發：恰好等於上下限不算異常。
    "vitals_alert_enabled": False,
    "vitals_hr_low": 50,
    "vitals_hr_high": 120,
    "vitals_rr_low": 10,
    "vitals_rr_high": 30,
    # IT-21: demo 通報用的獨立 Telegram bot／chat；空＝沿用正式 bot／收件人。
    # 設了 token 時直送 Telegram API，不經 notify hub。
    "demo_telegram_bot_token": "",
    "demo_telegram_chat_id": "",
    # IT-25: 電子白板（板榮 1F 開）。每床結果定案即 publish retained QoS1 到
    # bio-patrol/board/<whiteboard_site_id>/bed/<床號>；demo 一律不推。
    # site_id 空＝不推（開關開著會 WARN 一次）。床號預設原樣（如 101-1），
    # whiteboard_bed_id_map 可轉廠商床號，如 {"101-1": "A01"}。
    # online/LWT 綁在共享 MQTT 連線上，開關與 site_id 改了要重啟容器才生效。
    # 契約見 docs/whiteboard-board-topic.md。
    "whiteboard_enabled": False,
    "whiteboard_site_id": "",
    "whiteboard_bed_id_map": {},
    # F-53: 按鈕動作 speak（送護理站）用的貨架／目的地。空 shelf_id＝按鈕拒絕執行
    # （不再寫死板榮的 S05）。板榮下次 pull 前必須先在它的 settings.json 寫入
    # "ns_delivery_shelf_id": "S05"，否則該站的送護理站按鈕會變成拒絕。
    "ns_delivery_shelf_id": "",
    "ns_delivery_location_id": "NS",
    # F-24: 機器人每日例行重開窗 "HH:MM-HH:MM"（可跨午夜），空＝不檢查。值是各站
    # 實測（板榮約 02:50-03:20），新站先實測再填；排程儲存時據此檢查。
    "robot_reboot_window": "",
}

DEFAULT_BEDS = {
    "room_count": 14,
    "room_start": 101,
    "bed_numbers": [1],
    "beds": {},
}

DEFAULT_PATROL = {
    "beds_order": [],
}

DEFAULT_SCHEDULE = {
    "schedules": [],
}
