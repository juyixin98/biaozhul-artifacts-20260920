#!/usr/bin/env bash
# examples/curl_examples.sh — 用 curl 依次演示请求样例。
# 前置：已启动服务，例如
#   bin/licensejudge serve --policy configs/policy.json --addr 127.0.0.1:8080
# 可通过环境变量覆盖地址：BASE=http://127.0.0.1:9090 ./examples/curl_examples.sh
# 仅访问本机回环地址，不访问任何外部网络。
set -u

BASE="${BASE:-http://127.0.0.1:8080}"

call() {
  local title="$1"; shift
  printf '\n===== %s =====\n' "$title"
  curl -sS -i "$@" | sed 's/\r$//'
  printf '\n'
}

call "GET /healthz" \
  -H 'Accept: application/json' \
  "$BASE/healthz"

call "GET /v1/policies" \
  -H 'Accept: application/json' \
  "$BASE/v1/policies"

post() {
  local title="$1"; local expr="$2"
  call "$title" \
    -H 'Content-Type: application/json' \
    -d "{\"expression\":\"$expr\"}" \
    "$BASE/v1/evaluate"
}

post "OR 备选（allow，selection=MIT）" 'MIT OR GPL-3.0-only'
post "AND 拒绝（deny）" 'MIT AND GPL-3.0-only'
post "优先级 AND>OR" 'MIT OR GPL-3.0-only AND GPL-2.0-only'
post "括号改变分组" '(MIT OR GPL-3.0-only) AND GPL-2.0-only'
post "WITH 允许" 'LGPL-2.1-only WITH Classpath-exception-2.0'
post "WITH 绑定不匹配（deny）" 'MIT WITH Classpath-exception-2.0'
post "被拒许可证例外不能翻盘" 'GPL-3.0-only WITH Classpath-exception-2.0'
post "未知许可证（unknown，不自动通过）" 'Brand-New-Weird-License-9.9'
post "未知许可证+已知例外（unknown）" 'Brand-New-Weird-License-9.9 WITH Classpath-exception-2.0'
post "OR deny+unknown（unknown）" 'GPL-3.0-only OR Brand-New-Weird-License-9.9'
post "OR 全部拒绝（deny，无 selection）" 'GPL-2.0-only OR GPL-3.0-only OR SSPL-1.0'

printf '\n===== 解析错误（期望 400 parse_error） =====\n'
curl -sS -i -H 'Content-Type: application/json' \
  -d '{"expression":"MIT AND"}' "$BASE/v1/evaluate" | sed 's/\r$//'
printf '\n'
