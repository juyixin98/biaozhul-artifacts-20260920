#!/usr/bin/env bash
# 通用调用工具：对运动仲裁服务发起一次真实 HMAC-SHA256 签名的 HTTP 请求。
#
# 用法:
#   scripts/call.sh <autonomous|remote|estop|any> <METHOD> <PATH> [SIM_AT_MS] [JSON_BODY]
# 环境:
#   BASE_URL      默认 http://127.0.0.1:8080
#   KEY_AUTONOMOUS / KEY_REMOTE / KEY_ESTOP  默认开发密钥（须与服务端一致）
#
# 输出: HTTP 状态码与响应体；同时把签名过程打印到 stderr（密码学真实计算，无伪造）。
set -euo pipefail

KEY_NAME="${1:?usage: call.sh <autonomous|remote|estop|any> METHOD PATH [SIM_AT] [BODY]}"
METHOD="${2:?METHOD required}"
APIPATH="${3:?PATH required}"
SIM_AT="${4:-}"
BODY="${5:-}"

BASE_URL="${BASE_URL:-http://127.0.0.1:8080}"
KEY_AUTONOMOUS="${KEY_AUTONOMOUS:-dev-autonomous-secret}"
KEY_REMOTE="${KEY_REMOTE:-dev-remote-secret}"
KEY_ESTOP="${KEY_ESTOP:-dev-estop-secret}"

case "$KEY_NAME" in
  autonomous) KEY="$KEY_AUTONOMOUS" ;;
  remote)     KEY="$KEY_REMOTE" ;;
  estop)      KEY="$KEY_ESTOP" ;;
  any)        KEY="$KEY_ESTOP" ;; # any 仅用于 evaluate；默认 estop 密钥即可通过
  *) echo "unknown key: $KEY_NAME" >&2; exit 2 ;;
esac

# 与服务端完全一致的签名串: METHOD\nPATH\nX-Sim-At\nBODY
SIG_PAYLOAD="$(printf '%s\n%s\n%s\n%s' "$METHOD" "$APIPATH" "$SIM_AT" "$BODY")"
SIG="$(printf '%s' "$SIG_PAYLOAD" | openssl dgst -sha256 -hmac "$KEY" -hex | sed 's/^.*= //')"

echo "--- request -------------------------------------------------" >&2
echo "method : $METHOD  path: $APIPATH  sim_at: ${SIM_AT:-<none>}" >&2
echo "body   : ${BODY:-<empty>}" >&2
echo "signing: METHOD\\nPATH\\nX-SIM-AT\\nBODY with ${KEY_NAME} key -> $SIG" >&2
echo "-------------------------------------------------------------" >&2

TMP_BODY="$(mktemp)"
TMP_HDR="$(mktemp)"
trap 'rm -f "$TMP_BODY" "$TMP_HDR"' EXIT
printf '%s' "$BODY" > "$TMP_BODY"

ARGS=( -sS -o "$TMP_BODY" -D "$TMP_HDR" -w '%{http_code}'
       -X "$METHOD" "${BASE_URL}${APIPATH}"
       -H "X-Signature: hex=$SIG" )
[ -n "$SIM_AT" ] && ARGS+=( -H "X-Sim-At: $SIM_AT" )
[ -n "$BODY" ]   && ARGS+=( -H "Content-Type: application/json" --data-binary "@/dev/stdin" )

if [ -n "$BODY" ]; then
  CODE="$(printf '%s' "$BODY" | curl "${ARGS[@]}")"
else
  CODE="$(curl "${ARGS[@]}")"
fi

STATUS_LINE="$(head -1 "$TMP_HDR" | tr -d '\r')"
if [ "${QUIET:-0}" != "1" ]; then
  echo "HTTP $CODE ($STATUS_LINE)"
fi
if [ -s "$TMP_BODY" ]; then
  if [ "${RAW:-0}" = "1" ]; then cat "$TMP_BODY"; echo; else cat "$TMP_BODY" | jq . 2>/dev/null || cat "$TMP_BODY"; echo; fi
fi
# 以状态码退出，便于脚本断言
[ "$CODE" -ge 200 ] && [ "$CODE" -lt 300 ]
