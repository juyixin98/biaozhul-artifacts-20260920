#!/usr/bin/env bash
# 演示 1（主验收）：三节点同时存在中段 payload 损坏、错误父哈希、短段、
# 超时、gRPC 错误、虚高宣称高度。同步器必须换源重试、不跳缺口，
# 最终链与可信样例逐哈希一致，并记录完整来源证据。
set -euo pipefail
cd "$(dirname "$0")/.."

PORT_A=${PORT_A:-50051}
PORT_B=${PORT_B:-50052}
PORT_C=${PORT_C:-50053}
DB=run/demo1/sync.db
REPORT=run/demo1/report.json
rm -rf run/demo1 && mkdir -p run/demo1 bin
[ -f examples/fixture.json ] || go run ./cmd/genfixture -out examples/fixture.json

echo ">> 编译"
go build -o bin/genfixture ./cmd/genfixture
go build -o bin/stubnode  ./cmd/stubnode
go build -o bin/syncer     ./cmd/syncer

echo ">> 启动三个故障桩节点"
# node-a: 段0 超时；段3(24:32) payload 中段损坏；段6(48:56) gRPC 错误；宣称高度虚高 +10
./bin/stubnode -id node-a -addr 127.0.0.1:${PORT_A} \
  -timeout 0:8 -timeout-ms 3000 -corrupt-payload 24:32 -errors 48:56 -inflate-tip 10 \
  >run/demo1/a.log 2>&1 &
A=$!
# node-b: 段1(8:16) 错误父哈希；段4(32:40) 短段；段7(56:64) 超时
./bin/stubnode -id node-b -addr 127.0.0.1:${PORT_B} \
  -corrupt-parent 8:16 -short 32:40 -timeout 56:64 -timeout-ms 3000 \
  >run/demo1/b.log 2>&1 &
B=$!
# node-c: 段5(40:48) 超时；其余诚实
./bin/stubnode -id node-c -addr 127.0.0.1:${PORT_C} \
  -timeout 40:48 -timeout-ms 3000 \
  >run/demo1/c.log 2>&1 &
C=$!
trap 'kill $A $B $C 2>/dev/null || true' EXIT
sleep 1

echo ">> 执行同步（并行上限 4，乱序缓存 8 段，单段 4 次尝试）"
set +e
./bin/syncer -fixture examples/fixture.json -db "$DB" -report "$REPORT" \
  -peer node-a=127.0.0.1:${PORT_A} -peer node-b=127.0.0.1:${PORT_B} -peer node-c=127.0.0.1:${PORT_C}
RC=$?
set -e
echo ">> syncer 退出码: $RC（0=完整成功）"
[ "$RC" -eq 0 ]
echo ">> 验收通过：链尖哈希与可信样例一致，证据见 $REPORT 与 $DB"
