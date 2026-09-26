#!/usr/bin/env bash
# 可直接执行的范围语义演示。需要：bash、curl、python3。
# 用法：
#   go run ./cmd/rangeserver -addr 127.0.0.1:8080 &   # 先启动服务
#   bash examples/curl-examples.sh
set -u

BASE="${BASE:-http://127.0.0.1:8080}"
FAIL=0

# check <描述> <期望状态码> <实际状态码>
check() {
  local desc="$1" want="$2" got="$3"
  if [[ "$want" == "$got" ]]; then
    printf 'PASS  %-38s %s\n' "$desc" "$got"
  else
    printf 'FAIL  %-38s want=%s got=%s\n' "$desc" "$want" "$got"
    FAIL=1
  fi
}

code() { curl -s -o /tmp/rs_body.out -w '%{http_code}' "$@"; }

echo "== 基础 =="
check "full GET"                 200 "$(code "$BASE/artifacts/binary256")"
check "catalog"                  200 "$(code "$BASE/artifacts")"
check "unknown artifact"         404 "$(code "$BASE/artifacts/nope")"

echo "== 单范围 =="
check "prefix bytes=0-99"        206 "$(code -H 'Range: bytes=0-99' "$BASE/artifacts/binary256")"
check "suffix bytes=-64"         206 "$(code -H 'Range: bytes=-64' "$BASE/artifacts/binary256")"
check "open-ended bytes=200-"    206 "$(code -H 'Range: bytes=200-' "$BASE/artifacts/binary256")"
check "overlong clamps"          206 "$(code -H 'Range: bytes=250-9999' "$BASE/artifacts/binary256")"

echo "== 多范围 / 重叠 =="
check "tiled multipart"          206 "$(code -H 'Range: bytes=0-99,100-199,200-255' "$BASE/artifacts/binary256")"
check "overlapping coalesced"    206 "$(code -H 'Range: bytes=0-119,100-255' "$BASE/artifacts/binary256")"

echo "== 异常与条件 =="
check "unsatisfiable 416"        416 "$(code -H 'Range: bytes=1000-' "$BASE/artifacts/binary256")"
check "zero-length range 416"    416 "$(code -H 'Range: bytes=0-0' "$BASE/artifacts/empty")"
check "zero-length plain GET"    200 "$(code "$BASE/artifacts/empty")"
check "malformed ignored"        200 "$(code -H 'Range: bytes=9-1' "$BASE/artifacts/binary256")"
check "range limit 400"          400 "$(code -H 'Range: bytes=0-0,1-1,2-2,3-3,4-4,5-5' "$BASE/artifacts/binary256")"
check "stale If-Range"           200 "$(code -H 'Range: bytes=0-9' -H 'If-Range: "stale"' "$BASE/artifacts/binary256")"
check "If-Range future date"     206 "$(code -H 'Range: bytes=0-9' -H 'If-Range: Tue, 01 Jan 2030 00:00:00 GMT' "$BASE/artifacts/binary256")"
check "If-Range past date"       200 "$(code -H 'Range: bytes=0-9' -H 'If-Range: Sat, 01 Jan 2000 00:00:00 GMT' "$BASE/artifacts/binary256")"

echo "== 内容协商 =="
check "gzip representation"      200 "$(code -H 'Accept-Encoding: gzip' "$BASE/artifacts/lorem300")"
check "range against gzip rep"   206 "$(code -H 'Accept-Encoding: gzip' -H 'Range: bytes=0-9' "$BASE/artifacts/lorem300")"
check "406 unacceptable coding"  406 "$(code -H 'Accept-Encoding: identity;q=0, gzip;q=0' "$BASE/artifacts/binary256")"

echo "== Content-Range 抽查 =="
cr=$(curl -s -D - -o /dev/null -H 'Range: bytes=192-255' "$BASE/artifacts/binary256" | tr -d '\r' | awk 'tolower($1)=="content-range:"{print $2" "$3}')
[[ "$cr" == "bytes 192-255/256" ]] && echo "PASS  suffix Content-Range           $cr" || { echo "FAIL  Content-Range got $cr"; FAIL=1; }
cr=$(curl -s -D - -o /dev/null -H 'Range: bytes=9999-' "$BASE/artifacts/binary256" | tr -d '\r' | awk 'tolower($1)=="content-range:"{print $2" "$3}')
[[ "$cr" == "bytes */256" ]] && echo "PASS  416 Content-Range              $cr" || { echo "FAIL  Content-Range got $cr"; FAIL=1; }

echo
if [[ "$FAIL" == 0 ]]; then
  echo "ALL CURL CHECKS PASSED"
else
  echo "SOME CHECKS FAILED"
fi
exit "$FAIL"
