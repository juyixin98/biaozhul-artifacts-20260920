#!/bin/sh
# 三副本演示：写入 -> 分区 -> 并发操作 -> 恢复 -> 收敛
# 依赖：go、curl。先构建：go build -o orset .
set -e
cd "$(dirname "$0")/.."
go build -o /tmp/orset-demo .

/tmp/orset-demo -id node-1 -addr :8001 & P1=$!
/tmp/orset-demo -id node-2 -addr :8002 & P2=$!
/tmp/orset-demo -id node-3 -addr :8003 & P3=$!
trap 'kill $P1 $P2 $P3 2>/dev/null' EXIT
sleep 1

echo '== 初始写入并全网同步 =='
curl -s -XPOST localhost:8001/add -d '{"element":"shared"}'; echo
curl -s -XPOST localhost:8001/sync -d '{"peer":"http://localhost:8002"}'; echo
curl -s -XPOST localhost:8001/sync -d '{"peer":"http://localhost:8003"}'; echo

echo '== 分区：node-1 离线，node-2/node-3 互通 =='
curl -s -XPOST localhost:8001/add -d '{"element":"offline-1"}'; echo
curl -s -XPOST localhost:8001/remove -d '{"element":"shared"}'; echo
curl -s -XPOST localhost:8002/add -d '{"element":"online-2"}'; echo
curl -s -XPOST localhost:8003/add -d '{"element":"online-3"}'; echo
curl -s -XPOST localhost:8003/remove -d '{"element":"shared"}'; echo
curl -s -XPOST localhost:8002/sync -d '{"peer":"http://localhost:8003"}'; echo

echo '-- 分区期间各自视图（应当不同）--'
echo "node-1: $(curl -s localhost:8001/elements)"
echo "node-2: $(curl -s localhost:8002/elements)"

echo '== 分区恢复：两两同步两轮 =='
for i in 1 2; do
  curl -s -XPOST localhost:8001/sync -d '{"peer":"http://localhost:8002"}' > /dev/null
  curl -s -XPOST localhost:8002/sync -d '{"peer":"http://localhost:8003"}' > /dev/null
  curl -s -XPOST localhost:8001/sync -d '{"peer":"http://localhost:8003"}' > /dev/null
done

echo '-- 恢复后三个副本（应当完全一致）--'
echo "node-1: $(curl -s localhost:8001/elements)"
echo "node-2: $(curl -s localhost:8002/elements)"
echo "node-3: $(curl -s localhost:8003/elements)"
