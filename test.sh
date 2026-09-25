#!/usr/bin/env bash
# 运行全部自动化测试（先构建）。任一测试套件失败则整体退出非零。
set -uo pipefail
cd "$(dirname "$0")"

./build.sh

SUITES=(
  streamagg.tests.EngineTest
  streamagg.tests.PropertyTest
  streamagg.tests.ClockSchedulerTest
  streamagg.tests.JsonTest
  streamagg.tests.ServiceTest
)

FAILED=0
for suite in "${SUITES[@]}"; do
  echo
  echo "==================== $suite ===================="
  if java -cp build "$suite"; then
    echo ">>> $suite 通过"
  else
    echo ">>> $suite 失败"
    FAILED=1
  fi
done

echo
if [ "$FAILED" -eq 0 ]; then
  echo "========== 全部测试套件通过 =========="
else
  echo "========== 存在失败测试 =========="
fi
exit "$FAILED"
