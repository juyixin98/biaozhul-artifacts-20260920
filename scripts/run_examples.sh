#!/usr/bin/env bash
# 构建并运行全部请求样例，与 examples/expected/ 中的实际输出比对。
set -euo pipefail
cd "$(dirname "$0")/.."

JAR=target/interval-set-algebra-1.0.0.jar
mvn -o -q package

fail=0
for req in examples/requests/*.json; do
  name=$(basename "$req" .json)
  actual=$(java -jar "$JAR" "$req" | jq -S .)
  expected=$(cat "examples/expected/$name.json")
  if [ "$actual" == "$expected" ]; then
    echo "PASS $name"
  else
    echo "FAIL $name（实际输出与 examples/expected/$name.json 不一致）"
    fail=1
  fi
done
exit $fail
