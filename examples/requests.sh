#!/usr/bin/env bash
# segindex JSON API 请求样例。先启动服务：
#   java -jar target/segindex-1.0.0.jar --port 8080 --data ./index-data
set -euo pipefail
BASE="${BASE:-http://localhost:8080}"

echo '--- health ---'
curl -s "$BASE/health"; echo

echo '--- add documents ---'
curl -s -X POST "$BASE/docs" -d '{"id":"doc-1","text":"the quick brown fox jumps over the lazy dog"}'; echo
curl -s -X POST "$BASE/docs" -d '{"id":"doc-2","text":"quick silver is a movie about a bicycle messenger"}'; echo
curl -s -X POST "$BASE/docs" -d '{"id":"doc-3","text":"lazy afternoons and quiet evenings"}'; echo

echo '--- search: quick ---'
curl -s "$BASE/search?q=quick"; echo

echo '--- update doc-1 (same id, generation bumps to 2) ---'
curl -s -X POST "$BASE/docs" -d '{"id":"doc-1","text":"the quick red fox"}'; echo

echo '--- search: fox (returns generation 2 only) ---'
curl -s "$BASE/search?q=fox"; echo

echo '--- search: brown (existed only in generation 1, must be empty) ---'
curl -s "$BASE/search?q=brown"; echo

echo '--- delete doc-2 ---'
curl -s -X DELETE "$BASE/docs/doc-2"; echo

echo '--- search: quick after delete ---'
curl -s "$BASE/search?q=quick"; echo

echo '--- index status ---'
curl -s "$BASE/segments"; echo

echo '--- force merge ---'
curl -s -X POST "$BASE/merge"; echo

echo '--- error cases ---'
curl -s -o /dev/null -w 'POST /docs bad body -> %{http_code}\n' -X POST "$BASE/docs" -d '{"id":1}'
curl -s -o /dev/null -w 'GET /search without q -> %{http_code}\n' "$BASE/search"
