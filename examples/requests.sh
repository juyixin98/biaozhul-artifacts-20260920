#!/usr/bin/env bash
# 请求样例：对本地运行的服务打全部典型请求。
# 用法: ./examples/requests.sh [base-url]，默认 http://localhost:8080
set -euo pipefail
BASE="${1:-http://localhost:8080}"
HERE="$(cd "$(dirname "$0")" && pwd)"

say() { printf '\n==== %s ====\n' "$*"; }

say "GET / （服务信息）"
curl -s "$BASE/" | head -c 600; echo

say "GET /api/health"
curl -s "$BASE/api/health"; echo

say "POST /api/match 重叠 + 重复模式身份（she/he/hers + 重复 ab）"
curl -s -X POST "$BASE/api/match" -H 'Content-Type: application/json' -d '{
  "text": "ushers abab",
  "patterns": [
    {"id":"he","literal":"he"},
    {"id":"she","literal":"she"},
    {"id":"hers","literal":"hers"},
    {"id":"dup-ab-1","literal":"ab"},
    {"id":"dup-ab-2","literal":"ab"}
  ]
}'; echo

say "POST /api/match 空模式 MATCH_EVERY_POSITION（ab -> 3 个边界 + a 命中）"
curl -s -X POST "$BASE/api/match" -H 'Content-Type: application/json' \
  -d '{"text":"ab","emptyPatternPolicy":"MATCH_EVERY_POSITION","patterns":["a",{"id":"empty","literal":""}]}'; echo

say "POST /api/match 空模式 SKIP"
curl -s -X POST "$BASE/api/match" -H 'Content-Type: application/json' \
  -d '{"text":"ab","emptyPatternPolicy":"SKIP","patterns":["a",{"id":"empty","literal":""}]}'; echo

say "POST /api/match 空模式 ERROR（期望 HTTP 400）"
curl -s -o /dev/null -w 'HTTP %{http_code}\n' -X POST "$BASE/api/match" -H 'Content-Type: application/json' \
  -d '{"text":"ab","emptyPatternPolicy":"ERROR","patterns":["a",""]}'

say "POST /api/match Unicode（emoji 双坐标）"
curl -s -X POST "$BASE/api/match" -H 'Content-Type: application/json' \
  -d '{"text":"a😀b你好😀c","patterns":["😀","你好","😀c","a😀"]}'; echo

say "POST /api/match（请求体来自 examples/match-request.json）"
curl -s -X POST "$BASE/api/match" -H 'Content-Type: application/json' \
  --data-binary @"$HERE/match-request.json"; echo

say "GET /api/corpus?profile=sharedPrefix（不含正文）"
curl -s "$BASE/api/corpus?profile=sharedPrefix&textLength=120&patternCount=10&includeText=false"; echo

say "POST /api/corpus/run dna / 每块 7 code points（逐块统计 + 跨块命中）"
curl -s -X POST "$BASE/api/corpus/run" -H 'Content-Type: application/json' -d '{
  "profile":"dna","textLength":300,"patternCount":20,"chunkSize":7,"chunkUnit":"CODEPOINT"
}'; echo

say "POST /api/corpus/run unicode / 每块 3 UTF-8 字节（边界可切断 emoji）"
curl -s -X POST "$BASE/api/corpus/run" -H 'Content-Type: application/json' \
  --data-binary @"$HERE/corpus-run-request.json"; echo

# ---- 流式会话：模式 abcde 被切成 abc|de 两块 ----
say "POST /api/sessions 创建会话"
CREATE=$(curl -s -X POST "$BASE/api/sessions" -H 'Content-Type: application/json' \
  -d '{"patterns":[{"id":"whole","literal":"abcde"}],"emptyPatternPolicy":"SKIP"}')
echo "$CREATE"
ID=$(printf '%s' "$CREATE" | sed -n 's/.*"sessionId":"\([^"]*\)".*/\1/p')
echo "sessionId=$ID"

say "feed 第一块 abc（0 命中：模式尚未完成）"
curl -s -X POST "$BASE/api/sessions/$ID/feed" -H 'Content-Type: application/json' -d '{"chunk":"abc"}'; echo

say "feed 第二块 de（abcde 跨块命中，start=0）"
curl -s -X POST "$BASE/api/sessions/$ID/feed" -H 'Content-Type: application/json' -d '{"chunk":"de"}'; echo

say "finish（尾部为空）"
curl -s -X POST "$BASE/api/sessions/$ID/finish" -H 'Content-Type: application/json' -d '{}'; echo

# ---- UTF-8 字节流：单字节喂入 "😀x"，emoji 被切成 4 个字节 ----
say "POST /api/sessions 字节流：逐字节喂 😀x（模式 😀 跨 4 个字节块命中）"
CREATE2=$(curl -s -X POST "$BASE/api/sessions" -H 'Content-Type: application/json' \
  -d '{"patterns":[{"id":"emoji","literal":"😀"}],"emptyPatternPolicy":"SKIP"}')
ID2=$(printf '%s' "$CREATE2" | sed -n 's/.*"sessionId":"\([^"]*\)".*/\1/p')
python3 - "$BASE" "$ID2" <<'PY'
import base64, json, sys, urllib.request
base, sid = sys.argv[1], sys.argv[2]
for b in "😀x".encode("utf-8"):
    body = json.dumps({"bytesBase64": base64.b64encode(bytes([b])).decode()}).encode()
    req = urllib.request.Request(f"{base}/api/sessions/{sid}/feed", data=body,
                                 headers={"Content-Type": "application/json"})
    print(urllib.request.urlopen(req).read().decode())
req = urllib.request.Request(f"{base}/api/sessions/{sid}/finish", data=b"{}",
                             headers={"Content-Type": "application/json"})
print(urllib.request.urlopen(req).read().decode())
PY

say "错误样例：未知会话 404 / 错误策略 400 / 畸形 JSON 400"
curl -s -o /dev/null -w 'unknown session -> HTTP %{http_code}\n' \
  -X POST "$BASE/api/sessions/nope/feed" -H 'Content-Type: application/json' -d '{}'
curl -s -o /dev/null -w 'bad policy      -> HTTP %{http_code}\n' \
  -X POST "$BASE/api/match" -H 'Content-Type: application/json' \
  -d '{"text":"x","patterns":["x"],"emptyPatternPolicy":"WAT"}'
curl -s -o /dev/null -w 'malformed json  -> HTTP %{http_code}\n' \
  -X POST "$BASE/api/match" -H 'Content-Type: application/json' -d 'not-json'

echo
echo "all sample requests done."
