#!/usr/bin/env bash
# 运行一个请求样例：./run.sh samples/xxx.json [响应输出.json]
# 额外导出：--plan-out 计划文件  --data-out 数据文件  --dp-out DP表文件
set -euo pipefail
cd "$(dirname "$0")"
if [ ! -d build/classes ]; then
  ./build.sh >/dev/null
fi
java -cp build/classes joinorder.Main "$@"
