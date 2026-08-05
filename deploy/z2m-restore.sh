#!/bin/sh
# z2m-restore.sh — zigbee2mqtt 容器啟動前，把最新一代快照蓋回 /app/data。
#
# 現場 Pi 天天硬斷電，而 z2m 寫 configuration.yaml 是非原子的（fs.writeFileSync），
# 斷在寫入中就清零＝Zigbee 網路重生、按鈕全要重配。快照由 kiosk 後端在裝置清單
# 變動時存代（src/backend/zigbee/snapshot.py），這裡每次啟動無條件蓋回最新完整代：
# 正常開機蓋回的內容與現況相同，壞掉的那次開機才救得回來。
#
# 「不是 *.tmp 且有 meta.json」＝那一代寫完了。兩個條件缺一不可：快照端是先把
# 檔案（含 meta.json）寫進 gen-*.tmp/ 才 rename 成 gen-*，所以斷在 rename 前會留下
# 一個內容看起來很完整、但沒被認可的 .tmp——完成與否只認 rename。
# 可用性優先：任何情況都 exit 0，還原失敗絕不擋 z2m 啟動——但要留下痕跡：紀錄同時
# 寫「這代有哪些檔（expected）」與「實際蓋回哪些（files）」，兩者不等就 ok=0。少了
# 這組對照，「這代本來就沒有 coordinator_backup.json」和「cp 失敗」寫出來一模一樣，
# 沒還原成功的那次開機會長得跟健康的開機一樣（狀態卡照樣綠燈）。

SNAP_DIR="${SNAP_DIR:-/snapshots}"
DATA_DIR="${DATA_DIR:-/app/data}"
FILES="configuration.yaml database.db coordinator_backup.json"

# gen-<epochms> 的字典序＝時間序，走完迴圈留下的就是最新完整代。
gen=""
for d in "$SNAP_DIR"/gen-*; do
    case "$d" in *.tmp) continue ;; esac
    [ -f "$d/meta.json" ] || continue
    gen="$d"
done

if [ -z "$gen" ]; then
    echo "[restore] $SNAP_DIR 無完整快照，略過還原"
    exit 0
fi

name=$(basename "$gen")
expected=""
restored=""
for f in $FILES; do
    [ -f "$gen/$f" ] || continue
    expected="$expected${expected:+,}\"$f\""
    if cp "$gen/$f" "$DATA_DIR/$f.restore-tmp" \
        && mv "$DATA_DIR/$f.restore-tmp" "$DATA_DIR/$f"; then
        restored="$restored${restored:+,}\"$f\""
    else
        rm -f "$DATA_DIR/$f.restore-tmp"
        echo "[restore] $f 還原失敗"
    fi
done

# 兩份清單同序組出，字串相等＝這代的檔全數蓋回。
ok=1
[ "$expected" = "$restored" ] || ok=0
if [ "$ok" = 1 ]; then
    echo "[restore] 已從 $name 還原：${restored:-（無檔案）}"
else
    echo "[restore] 從 $name 還原不完整：預期 [$expected]，實際 [$restored]"
fi
printf '{"ts":%s,"gen":"%s","ok":%s,"expected":[%s],"files":[%s]}\n' \
    "$(date +%s)" "$name" "$ok" "$expected" "$restored" >> "$DATA_DIR/restore-log.jsonl"
exit 0
