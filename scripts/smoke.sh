#!/usr/bin/env bash
# 端到端冒烟脚本：启动服务（若未启动）→ 打各类 Range 请求 → 打印结论。
set -u

HOST=${RANGE_HOST:-127.0.0.1}
PORT=${RANGE_PORT:-3000}
BASE="http://${HOST}:${PORT}"

if ! curl -s -o /dev/null "${BASE}/objects"; then
  echo "[smoke] 服务未运行，先执行 npm start（或 npm run seed && npm start）" >&2
  exit 1
fi

check() {
  local name="$1"; shift
  local expected="$1"; shift
  local actual
  actual=$(curl -s -o /dev/null -w '%{http_code}' "$@")
  if [ "$actual" = "$expected" ]; then
    echo "PASS  ${name}: HTTP ${actual}"
  else
    echo "FAIL  ${name}: 期望 ${expected}，实际 ${actual}"
    return 1
  fi
}

check "完整 GET" 200 "${BASE}/objects/alphabet"
check "单范围 206" 206 -H 'Range: bytes=0-4' "${BASE}/objects/alphabet"
check "开放结尾 206" 206 -H 'Range: bytes=20-' "${BASE}/objects/alphabet"
check "后缀范围 206" 206 -H 'Range: bytes=-4' "${BASE}/objects/alphabet"
check "结束越界裁剪 206" 206 -H 'Range: bytes=24-999' "${BASE}/objects/alphabet"
check "起始越界 416" 416 -H 'Range: bytes=26-' "${BASE}/objects/alphabet"
check "零长度对象 416" 416 -H 'Range: bytes=0-0' "${BASE}/objects/zero-empty"
check "语法错误 400" 400 -H 'Range: bytes=abc' "${BASE}/objects/alphabet"
check "多范围 206" 206 -H 'Range: bytes=0-2,23-25' "${BASE}/objects/alphabet"
check "相邻合并（非 multipart）206" 206 -H 'Range: bytes=0-4,5-9' "${BASE}/objects/alphabet"

# If-Range：先取对象真实强 ETag，匹配应 206，错误 ETag 应回退 200。
REAL_ETAG=$(curl -s -D- -o /dev/null "${BASE}/objects/alphabet" | tr -d '\r' | awk -F': ' 'tolower($1)=="etag"{print $2}')
check "If-Range 匹配 206" 206 -H 'Range: bytes=0-2' -H "If-Range: ${REAL_ETAG}" "${BASE}/objects/alphabet"
check "If-Range 不匹配回 200" 200 -H 'Range: bytes=0-2' -H 'If-Range: "stale-etag"' "${BASE}/objects/alphabet"
check "对象不存在 404" 404 "${BASE}/objects/no-such-object"

echo
echo "multipart 原始响应（0-2 与 23-25）："
curl -s -H 'Range: bytes=0-2,23-25' "${BASE}/objects/alphabet"
echo
echo "最近 3 条诊断记录："
curl -s "${BASE}/_diagnostics/records?limit=3"
