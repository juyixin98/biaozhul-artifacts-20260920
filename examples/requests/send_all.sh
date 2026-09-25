#!/usr/bin/env bash
# 发送所有样例请求到本地 ResFlow 服务。
# 用法：先启动服务  python3 -m resflow.cli serve --port 8080
#       然后运行     bash examples/requests/send_all.sh
set -u
HOST="${RESFLOW_HOST:-127.0.0.1}"
PORT="${RESFLOW_PORT:-8080}"
BASE="http://${HOST}:${PORT}"
HERE="$(cd "$(dirname "$0")" && pwd)"

echo "== GET /health =="
curl -s "${BASE}/health"; echo

for f in analyze_exceptional analyze_partial_init analyze_loop \
         analyze_try_catch analyze_clean; do
  echo
  echo "== POST /analyze  (${f}.json) =="
  curl -s -X POST "${BASE}/analyze" \
    -H 'Content-Type: application/json' \
    --data @"${HERE}/${f}.json" \
    | python3 -c '
import json, sys
r = json.load(sys.stdin)
if "error" in r:
    print("ERROR:", r["error"])
else:
    for fn in r["functions"]:
        print(fn["name"], fn["summary"])
        for d in fn["diagnostics"]:
            print("  -", d["code"], d["resource"],
                  "L%d:%d" % (d["location"]["line"],
                              d["location"]["column"]),
                  "paths", d["path_ids"], d["terminals"])
'
done

echo
echo "== POST /analyze 语法错误样例（期望 HTTP 400） =="
curl -s -o /tmp/resflow_bad.json -w "HTTP %{http_code}\n" \
  -X POST "${BASE}/analyze" -H 'Content-Type: application/json' \
  --data @"${HERE}/analyze_syntax_error.json"
cat /tmp/resflow_bad.json; echo
