#!/usr/bin/env bash
# 端到端演示：格式化 → 若干写入/覆盖/删除 → 重启验证持久化 → 崩溃自检。
# 仅依赖 curl 与本项目二进制；默认监听 127.0.0.1:8080。
set -euo pipefail

ADDR="${ADDR:-127.0.0.1:8080}"
DIR="${DIR:-./storedata}"
BIN="${BIN:-./target/release/dual-superblock}"

c() { curl -fsS "http://${ADDR}$@"; }
cput() { curl -fsS -X PUT   "http://${ADDR}$1" --data-binary "@-"; }
cdel() { curl -fsS -X DELETE "http://${ADDR}$1"; }

echo ">> 构建"
cargo build --release

echo ">> 清空并启动服务: $ADDR (dir=$DIR)"
rm -rf "$DIR"
"$BIN" serve --addr "$ADDR" --dir "$DIR" &
SRV=$!
trap 'kill $SRV 2>/dev/null || true' EXIT

# 等待端口就绪
for _ in $(seq 1 50); do
  curl -fsS "http://${ADDR}/health" >/dev/null 2>&1 && break
  sleep 0.1
done

echo ">> health"; c /health; echo
echo ">> 写 user:1=alice (gen1)"; printf 'alice' | cput /kv/user:1; echo
echo ">> 写 color=blue (gen2)"; printf 'blue'  | cput /kv/color; echo
echo ">> 批量写 a=1,b=2 (gen3)"
curl -fsS -X PUT "http://${ADDR}/kv" \
  --data '{"items":[{"key":"a","value":"1"},{"key":"b","value":"2"}]}'; echo
echo ">> 覆盖 color=red (gen4)"; printf 'red' | cput /kv/color; echo
echo ">> 读取 color"; c /kv/color; echo
echo ">> 删除 a (gen5)"; cdel /kv/a >/dev/null; echo "deleted"
echo ">> 列出全部"; c /kv; echo
echo ">> 快照"; c /snapshot; echo

echo ">> 重启服务（模拟进程重启，验证持久化）"
kill $SRV; wait $SRV 2>/dev/null || true
"$BIN" serve --addr "$ADDR" --dir "$DIR" &
SRV=$!
for _ in $(seq 1 50); do
  curl -fsS "http://${ADDR}/health" >/dev/null 2>&1 && break
  sleep 0.1
done
echo ">> 重启后快照（generation 应仍为 5）"; c /snapshot; echo
echo ">> 重启后 color 仍为 red"; c /kv/color; echo

echo ">> 崩溃恢复自检（模拟磁盘，19 项）"
code=$(curl -s -o /tmp/dsb-selftest.out -w '%{http_code}' "http://${ADDR}/admin/selftest")
tail -n 3 /tmp/dsb-selftest.out
[ "$code" = "200" ] || { echo "自检未通过 (HTTP $code)\n$(cat /tmp/dsb-selftest.out)"; exit 1; }

echo ">> 全部演示完成。"
