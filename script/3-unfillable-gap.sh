#!/usr/bin/env bash
# 演示 3（缺口不可跳过）：高度区间 24:32 在三个节点上分别
# payload 损坏 / 错误父哈希 / gRPC 错误 —— 没有任何源能提供好段。
# 同步必须失败（退出码 1），检查点停在缺口前（23），缺口之后的高度一律不得入库；
# 远端宣称的 63 高度不得被当作已验证进度。
set -euo pipefail
cd "$(dirname "$0")/.."

PORT_A=${PORT_A:-50071}
PORT_B=${PORT_B:-50072}
PORT_C=${PORT_C:-50073}
DB=run/demo3/sync.db
REPORT=run/demo3/report.json
rm -rf run/demo3 && mkdir -p run/demo3 bin
[ -f examples/fixture.json ] || go run ./cmd/genfixture -out examples/fixture.json
go build -o bin/stubnode ./cmd/stubnode
go build -o bin/syncer    ./cmd/syncer

echo ">> 启动节点：区间 24:32 三源全部不可用（不同故障类型）"
./bin/stubnode -id node-a -addr 127.0.0.1:${PORT_A} -corrupt-payload 24:32 \
  >run/demo3/a.log 2>&1 &
A=$!
./bin/stubnode -id node-b -addr 127.0.0.1:${PORT_B} -corrupt-parent 24:32 \
  >run/demo3/b.log 2>&1 &
B=$!
./bin/stubnode -id node-c -addr 127.0.0.1:${PORT_C} -errors 24:32 \
  >run/demo3/c.log 2>&1 &
C=$!
trap 'kill $A $B $C 2>/dev/null || true' EXIT
sleep 1

echo ">> 执行同步（预期失败，退出码 2=已保留连续前缀 0..23）"
set +e
./bin/syncer -fixture examples/fixture.json -db "$DB" -report "$REPORT" \
  -peer node-a=127.0.0.1:${PORT_A} -peer node-b=127.0.0.1:${PORT_B} -peer node-c=127.0.0.1:${PORT_C} \
  | tee run/demo3/run.txt
RC=${PIPESTATUS[0]}
set -e
echo ">> syncer 退出码: $RC"
[ "$RC" -eq 2 ]

echo ">> 校验检查点停在 23，且报告如实标记未到达目标"
grep -q "已验证检查点高度 : 23" run/demo3/run.txt
grep -q "同步未完全成功" run/demo3/run.txt
grep -q "不能跳过缺口" run/demo3/run.txt
echo ">> 缺口不可跳过演示通过（失败被如实报告，连续前缀 0..23 保留）"
