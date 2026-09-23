#!/usr/bin/env bash
# 批量运行 examples/ 下的全部模拟请求，并把响应写入 examples/output/。
set -euo pipefail
cd "$(dirname "$0")/.."

mkdir -p examples/output
for f in examples/0*.json; do
  name="$(basename "$f" .json)"
  echo ">>> $name"
  ./mvnw -q exec:java \
    -Dexec.mainClass=com.example.wm.service.HttpService \
    -Dexec.args="run $f examples/output/$name.response.json"
done
echo "全部响应已写入 examples/output/"
