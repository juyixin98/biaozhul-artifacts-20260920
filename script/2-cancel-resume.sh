#!/usr/bin/env bash
# 演示 2（取消竞争 + 检查点续传）：
#   第 1 轮：node-a 对段 3(24:32) 延迟 6s（该段首轮恰好打到 node-a）。
#           段 0/1/2 快速连续提交（检查点 15），段 3 在途；300ms 时主动取消。
#           取消后等待在途旧请求返回（>6s），证明旧结果绝不推进检查点。
#   第 2 轮：用同一个 SQLite 文件重新运行（模拟重启/恢复），从检查点 15 续传至 63，
#           最终链仍与可信样例一致。
set -euo pipefail
cd "$(dirname "$0")/.."

PORT_A=${PORT_A:-50061}
PORT_B=${PORT_B:-50062}
PORT_C=${PORT_C:-50063}
DB=run/demo2/sync.db
rm -rf run/demo2 && mkdir -p run/demo2 bin
[ -f examples/fixture.json ] || go run ./cmd/genfixture -out examples/fixture.json
go build -o bin/stubnode ./cmd/stubnode
go build -o bin/syncer    ./cmd/syncer

echo ">> 启动节点：node-a 对段 3(24:32) 延迟 6s（取消时仍在途），其余全部诚实"
./bin/stubnode -id node-a -addr 127.0.0.1:${PORT_A} -timeout 24:32 -timeout-ms 6000 \
  >run/demo2/a.log 2>&1 &
A=$!
./bin/stubnode -id node-b -addr 127.0.0.1:${PORT_B} >run/demo2/b.log 2>&1 &
B=$!
./bin/stubnode -id node-c -addr 127.0.0.1:${PORT_C} >run/demo2/c.log 2>&1 &
C=$!
trap 'kill $A $B $C 2>/dev/null || true' EXIT
sleep 1

PEERS="-peer node-a=127.0.0.1:${PORT_A} -peer node-b=127.0.0.1:${PORT_B} -peer node-c=127.0.0.1:${PORT_C}"

echo ">> 第 1 轮：运行 300ms 后取消，rpc-timeout=8s 保证旧请求跨越取消点仍在途"
set +e
./bin/syncer -fixture examples/fixture.json -db "$DB" -report run/demo2/report1.json \
  -rpc-timeout 8s -cancel-after 300ms $PEERS > run/demo2/run1.txt 2>&1
RC1=$?
set -e
echo ">> 第 1 轮退出码: $RC1（2=连续前缀已提交后中断，属预期）"
cat run/demo2/run1.txt | sed -n '1,12p'
grep -q "被取消: true" run/demo2/run1.txt
grep -q "已验证检查点高度 : 23" run/demo2/run1.txt

echo ">> 等待 6.5s，让取消前发出的在途旧请求全部返回……（检查点必须纹丝不动）"
sleep 6.5

echo ">> 第 2 轮：同一数据库文件重跑（模拟进程重启），应从检查点 23 续传到 63"
./bin/syncer -fixture examples/fixture.json -db "$DB" -report run/demo2/report2.json $PEERS \
  | tee run/demo2/run2.txt
grep -q "链尖与样例一致   : true" run/demo2/run2.txt
grep -q "已验证检查点高度 : 63" run/demo2/run2.txt
echo ">> 取消竞争 + 重启续传演示通过"
