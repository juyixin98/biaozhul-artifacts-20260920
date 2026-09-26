#!/usr/bin/env bash
# 用 examples/requests 下的样例文件逐个发起请求，演示 JSON 输入输出。
# 用法：./examples/curl_examples.sh [BASE_URL]
set -u
BASE="${1:-http://localhost:8088}"
DIR="$(dirname "$0")"
post(){ printf '\n>>> POST %s  < %s\n' "$1" "$2"; curl -s -X POST "$BASE$1" -H 'Content-Type: application/json' --data-binary @"$2" | jq -c '{success,data:(.data|if type=="object" then {id,status,deadlineWall,remainingMonotonicNanos} else . end),error:.error.message}'; }

printf '== 服务与 tzdb 信息 =='
curl -s "$BASE/info" | jq '.data | {clockType,tzdbVersion,javaVersion,wallNow,monoNowNanos}'

post /timeouts "$DIR/requests/01-schedule-duration-seconds.json"
post /timeouts "$DIR/requests/02-schedule-duration-iso.json"
post /timeouts "$DIR/requests/03-schedule-absolute.json"
post /timeouts "$DIR/requests/04-schedule-daily-local.json"
post /timeouts "$DIR/requests/05-schedule-version-ttl.json"
post /timeouts "$DIR/requests/06-schedule-already-past.json"

printf '\n== 列出全部 ==\n'
curl -s "$BASE/timeouts" | jq -r '.data[] | [.id,.status,.deadlineWall,(.remainingMonotonicNanos|tostring)] | @tsv'
