#!/usr/bin/env bash
# 运行验收评测：固定种子数据、多预算召回率、距离计算次数、过滤与删除校验。
# 文本表格打印到 stdout，同时把机读 JSON 写入 results/evaluation-report.json
set -euo pipefail
cd "$(dirname "$0")/.."

"$(dirname "$0")/build.sh" >/dev/null

if [ -n "${JAVA_HOME:-}" ]; then JAVA="$JAVA_HOME/bin/java"; else JAVA="${JAVA:-java}"; fi

mkdir -p results
# 文本报告留存；JSON 段单独提取到文件
$JAVA -cp build/classes:build/test-classes vecsearch.eval.Evaluation | tee results/evaluation-output.txt
# 从输出中抽取 JSON 段（起止标记之间）单独存盘
sed -n '/^-------- machine-readable JSON --------$/,/^ACCEPTANCE/p' results/evaluation-output.txt \
  | sed '1d;$d' > results/evaluation-report.json
echo "report saved to results/evaluation-report.json"
