#!/usr/bin/env bash
# 运行 samples/requests 下全部请求样例，响应与执行计划写入 samples/responses。
# 打印每个样例的退出码（业务错误样例 03/05 退出码为 1 是预期行为）。
set -uo pipefail
cd "$(dirname "$0")"
mkdir -p samples/responses
for f in samples/requests/*.json; do
  base="$(basename "$f" .json)"
  java -cp build/classes dev.dedup.hll.Main \
      --plan-out "samples/responses/$base.plan.json" \
      "$f" > "samples/responses/$base.response.json" 2>/dev/null
  echo "$base -> exit=$? (samples/responses/$base.response.json)"
done
