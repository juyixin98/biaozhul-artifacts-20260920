#!/usr/bin/env bash
# seglog 演示:基本读写 → 段滚动 → 崩溃注入 → 恢复 → 损坏拒绝启动
# 用法: cargo build && ./examples/demo.sh
set -uo pipefail

BIN=./target/debug/seglog
DIR=/tmp/seglog-demo
ADDR=127.0.0.1:3902
export SEGLOG_ADDR=$ADDR SEGLOG_DATA_DIR=$DIR SEGLOG_MAX_SEGMENT=200

[ -x "$BIN" ] || { echo "请先 cargo build"; exit 1; }
pkill -x seglog 2>/dev/null; rm -rf "$DIR"; sleep 0.3

step() { echo; echo "=== $* ==="; }

step "1. 启动服务(段上限 200 字节,便于演示滚动)"
$BIN >/tmp/seglog-demo.log 2>&1 &
SRV=$!; sleep 0.7

step "2. 追加记录"
for p in user-login add-to-cart checkout; do
  curl -s -X POST $ADDR/logs/events/records -H 'Content-Type: application/json' \
    -d "{\"payload\":\"$p\"}"; echo
done

step "3. 读取 / 列表 / 状态"
curl -s $ADDR/logs/events/records/2; echo
curl -s $ADDR/logs/events/records; echo
curl -s $ADDR/logs/events/status; echo

step "4. 继续写入触发段滚动"
for i in 4 5 6 7 8; do
  curl -s -X POST $ADDR/logs/events/records -H 'Content-Type: application/json' \
    -d "{\"payload\":\"event-$i\"}" >/dev/null
done
curl -s $ADDR/logs/events/status; echo
ls -l "$DIR"/events/

step "5. 崩溃注入: SEGLOG_CRASH=after_commit(已持久化,应答前崩溃)"
kill $SRV 2>/dev/null; sleep 0.3
SEGLOG_CRASH=after_commit $BIN >/tmp/seglog-crash.log 2>&1 &
CRASH_PID=$!; sleep 0.7
curl -s -X POST $ADDR/logs/events/records -H 'Content-Type: application/json' \
  -d '{"payload":"payment-done"}'; echo "  (curl 退出码 $? —— 应答丢失)"
sleep 0.5
wait $CRASH_PID; echo "服务退出码: $? (137 = 崩溃注入)"

step "6. 重启恢复: 未确认记录允许存在,序号不复用"
$BIN >/tmp/seglog-recover.log 2>&1 &
SRV=$!; sleep 0.7
curl -s $ADDR/logs/events/records; echo
curl -s -X POST $ADDR/logs/events/records -H 'Content-Type: application/json' \
  -d '{"payload":"after-recovery"}'; echo
kill $SRV 2>/dev/null; sleep 0.3

step "7. 中段损坏: 拒绝启动(不静默跳过)"
python3 -c "
import pathlib
p = pathlib.Path('$DIR/events/0000000000000000.seg')
b = bytearray(p.read_bytes()); b[30] ^= 0xFF; p.write_bytes(bytes(b))
print('已损坏', p, '偏移 30')"
$BIN 2>&1 | head -2
echo "服务退出码: ${PIPESTATUS[0]} (非零 = 拒绝启动)"
