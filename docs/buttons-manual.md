# Zigbee Button Hub — Operator & Setup Manual

This manual covers the Zigbee button hub: how to wire up the hardware, how to pair an SNZB-01P button to a bio-patrol action, and how to recover from common failure modes.

The user-facing sections (pairing, daily use, troubleshooting) match the Traditional Chinese strings used in the bio-patrol UI. Setup-host commands stay in English.

---

## 1. Hardware checklist

| Item | Notes |
|------|-------|
| SONOFF Zigbee 3.0 USB Dongle Plus **V2** | Itead. Must be the **V2** (EFR32MG21 chip / EmberZNet 7.4.4 / EZSP v13). The V1 (CC2652P) is not what this guide assumes. |
| SONOFF SNZB-01P button | One per action you want to pair. Up to 6 (one per action key). CR2477 battery pre-installed. |
| Edge host with USB | Raspberry Pi 5 in our reference deployment. Any Linux box that can run Docker works. |

---

## 2. First-time host setup

The Zigbee dongle shows up as `/dev/ttyUSB*` on the host, but the device number can change between reboots and between dongle types. Pin it to a stable name with udev so the `zigbee2mqtt` container can find it.

### 2.1 udev rule for `/dev/zigbee`

Run on the host (not inside any container):

```bash
# Find the dongle's idVendor / idProduct
lsusb | grep -i sonoff
# Typical output:
#   ID 1a86:55d4 QinHeng Electronics Sonoff Zigbee 3.0 USB Dongle Plus V2

sudo tee /etc/udev/rules.d/99-zigbee.rules >/dev/null <<'EOF'
# SONOFF Zigbee 3.0 USB Dongle Plus V2
SUBSYSTEM=="tty", ATTRS{idVendor}=="1a86", ATTRS{idProduct}=="55d4", SYMLINK+="zigbee"
EOF

sudo udevadm control --reload-rules
sudo udevadm trigger
ls -l /dev/zigbee  # should symlink to /dev/ttyUSB0 (or similar)
```

> Match `idVendor` / `idProduct` against your own `lsusb` output — the values above are for the V2 dongle. If you swap to a different dongle, update the rule.

### 2.2 Bring up the stack

```bash
docker compose up -d
docker compose logs -f zigbee2mqtt
```

Look for:

```
Zigbee Herdsman: starting with adapter 'ezsp'
Started server on port 8080
```

If the log says it cannot find `/dev/zigbee`, the udev rule has not applied — re-check section 2.1.

### 2.3 Verify bio-patrol picked up Zigbee

```bash
curl -s localhost:8000/api/button-bindings | jq '.pair_status'
# Expect:
# {
#   "armed_action": null,
#   "armed_remaining_s": null,
#   "mqtt_connected": true
# }
```

If `mqtt_connected` is `false`, the bio-patrol container cannot reach `mqtt-broker:1883` — check `docker compose ps` and the `bio-patrol-net` network.

---

## 3. 配對按鈕（Pair a button）

每個動作（`demo_run`、`shelf_resume`、`patrol_start`、`patrol_cancel`、`return_home`、`speak`）最多綁定一顆按鈕。一顆按鈕一次只能對應一個動作 — 重新配對到別的動作會自動清除舊綁定。

1. 開啟 bio-patrol 介面 → 切到 **Settings** 分頁。
2. 滾到 **Zigbee Buttons** 區塊。確認右上角顯示 **MQTT 已連線**（綠色），若為紅色「MQTT 未連線」請先解決連線問題（見第 6 節）。
3. 在想要綁定的動作那一列點 **配對**。該列會顯示「配對中… 120s」倒數。
4. 拿著 SNZB-01P 按鈕，**長按主鍵約 5 秒** — LED 閃一下後放開。
5. 等待約 5–15 秒。配對成功後，該列會更新成 IEEE 位址（例如 `0x1234567890abcdef`）、電量百分比、最後上線時間。
6. 立刻按一下按鈕測試。bio-patrol 應該觸發對應動作（同時可以看到該列「已觸發 1 次」）。

### 重要：長按時間

| 長按秒數 | 行為 | 你想要的？ |
|---------|------|------|
| ~5 秒 | 進入配對模式（rejoin） | 是 — 這是日常配對 |
| ~10 秒以上 | **原廠重設** | **不是** — 按鈕會清掉裡面所有 Zigbee 設定，得從 UI 重新開「配對」流程才能再次加入網路 |

如果不小心按到 10 秒，按鈕還是可以救回來 — 只是要重新走一次 UI 配對流程，不會壞掉。

---

## 4. 日常使用筆記

- **電池**：CR2477 鈕扣電池一顆通常可以撐 1 年以上（按鈕大部分時間是深度睡眠）。Settings 介面會顯示最後一次回報的電量；低於 20% 建議更換。
- **顯示「離線」可能是正常的**：按完之後 Settings 介面有時會把按鈕標成離線，下次按下去 `last_seen` 又會更新。優先看 `last_seen` 是否在你按下那一刻被更新；若有，按鈕運作正常。
- **重複按壓**：bio-patrol 端有 0.3 秒 debounce，一秒內按多下只會觸發一次動作。
- **同一顆按鈕想換動作**：只要在新動作那一列點 **配對** 並重新長按按鈕即可，舊綁定會自動釋放。
- **解除配對**：在已配對的列點 **解除配對**，會同時讓 z2m 把該裝置移除。下次想再用這顆按鈕得從第 3 節重來一次。
- **Test 按鈕**：每列的 **測試** 按鈕會直接呼叫該動作 handler（不需要實體按壓），方便驗證 bio-patrol 端設定是否正確。

---

## 5. 動作說明

| 動作 key | 觸發內容 | 備註 |
|---------|---------|------|
| `demo_run` | 啟動 demo 巡房 | 等同 Dashboard 的 Demo Run 按鈕 |
| `shelf_resume` | 自動找到最新一筆貨架掉落任務並繼續巡房 | 不需要 task_id，按一下就好 |
| `patrol_start` | 啟動正式巡房 | 用目前儲存的 patrol 路線 |
| `patrol_cancel` | 取消當下執行中的任務 | 沒有任務時回傳錯誤 |
| `return_home` | 機器人回充電座 | 會放下貨架 |
| `speak` | 機器人說「こんにちは、シグマです」 | 預設文字，目前不可從 UI 修改 |

---

## 6. 故障排除（Troubleshooting）

### 6.1「按鈕完全沒反應」

按下去 LED 沒亮、bio-patrol 也沒任何 log：

1. 換電池。電量耗盡時通常完全不亮燈。
2. 確認該按鈕在 Settings → Zigbee Buttons 已配對（有顯示 IEEE）。如果沒有就是還沒配對成功。
3. 檢查 z2m 容器健康：
   ```bash
   docker compose logs --tail 100 zigbee2mqtt | grep -i error
   ```
4. 把按鈕拿到離 dongle 2 公尺以內測試 — 如果近距離可以、遠距離不行，就是 Zigbee 訊號問題（牆面遮蔽、router 干擾）。Zigbee channel 預設 11，可在 `zigbee2mqtt/configuration.yaml` 裡換成 15 / 20 / 25 試。

### 6.2「按鈕一直顯示『離線』，但實際按下去能觸發」

**請以 `last_seen` 欄位為準。** 看 Settings UI 上該列的 `最後上線`：如果按下去那一刻會更新，按鈕就是運作正常的。離線標籤本身不是故障訊號。

### 6.3「按了好幾下都沒觸發 bio-patrol，但 z2m 看得到 message」

開兩個終端機分別看：

```bash
# Terminal A: z2m 收到的所有訊息
docker compose logs -f zigbee2mqtt | grep -i "MQTT publish"

# Terminal B: bio-patrol 端的 button_manager
docker compose logs -f app | grep -E "button_manager|action_registry"
```

按下實體按鈕時，A 應該有一筆 `zigbee2mqtt/<ieee>` 的 publish，B 應該緊接著有 `Firing action <key>` log。常見斷點：

- A 有訊息、B 沒有 → bio-patrol 跟 `mqtt-broker` 失聯。`docker compose restart app`。
- A、B 都沒訊息 → z2m 跟 dongle 失聯。看 `docker compose logs zigbee2mqtt` 有沒有 `serial` / `adapter` 錯誤；最常見的是 `/dev/zigbee` udev rule 沒生效（見 2.1）。
- B 有 `Press from unbound device 0x... ignoring` → 這顆按鈕加到 z2m 了但 bio-patrol 那邊沒有 binding 紀錄。在 UI 解除配對 → 再配對一次。

### 6.4「想配對的時候按按鈕、什麼都沒發生」

排查順序：

1. 確認你**先**在 UI 點了 **配對**，看到該列變成「配對中… N s」倒數。沒先點配對，按按鈕不會生效。
2. 確認長按是 **5 秒**而不是 1–2 秒輕按，也不要按到 10+ 秒（10 秒以上會原廠重設，這次配對會失敗，但下次可以再試）。
3. 視窗倒數結束（120 秒）都沒抓到 → 直接重新點 **配對** 再長按一次。

### 6.5「Test 按鈕成功，但實體按壓沒反應」

代表 bio-patrol → 動作 handler 路徑是對的，問題在 Zigbee → bio-patrol：

1. 看 `docker compose logs app | grep button_manager`。每次實體按壓都應該有一筆 `Firing action <key>`。
2. 沒看到 → 看 `docker compose logs zigbee2mqtt | tail -50` 確認 z2m 有收到 message。
3. z2m 也沒收到 → 確認 z2m 容器和 dongle 都活著（`docker compose ps`、`ls /dev/zigbee`）。

---

## 7. 快速指令對照

```bash
# 看目前所有按鈕綁定
curl -s localhost:8000/api/button-bindings | jq

# 用 API 直接觸發某個動作（等同 UI 上的「測試」）
curl -X POST localhost:8000/api/button-bindings/return_home/test

# 開始配對某動作（等同 UI 上的「配對」）
curl -X POST localhost:8000/api/button-bindings/demo_run/pair

# 取消配對
curl -X POST localhost:8000/api/button-bindings/demo_run/pair/cancel

# 解除已配對的按鈕（並從 z2m 移除）
curl -X DELETE 'localhost:8000/api/button-bindings/demo_run?forget_device=true'

# 看 z2m 即時 log
docker compose logs -f zigbee2mqtt

# 看 bio-patrol button 相關 log
docker compose logs -f app | grep -E 'button_manager|action_registry|zigbee_mqtt'
```
