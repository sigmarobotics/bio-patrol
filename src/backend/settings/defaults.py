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
