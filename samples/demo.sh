#!/usr/bin/env bash
# 端到端样例：走通一个 P1 事件从检测到关闭的完整生命周期。
# 用法: BASE=http://localhost:18082 ./samples/demo.sh   （默认 18082，即 compose 映射端口）
set -euo pipefail

BASE="${BASE:-http://localhost:18082}"
ADMIN="00000000-0000-0000-0000-0000000000a1"
ANALYST="00000000-0000-0000-0000-0000000000a2"
RESPONDER="00000000-0000-0000-0000-0000000000b1"

req() { # req <method> <path> <user> [json]
  local method="$1" path="$2" user="$3" body="${4:-}"
  if [ -n "$body" ]; then
    curl -sS -X "$method" "$BASE$path" -H "X-User-Id: $user" -H "Content-Type: application/json" -d "$body"
  else
    curl -sS -X "$method" "$BASE$path" -H "X-User-Id: $user"
  fi
  echo
}

echo "== 1. 创建 P1 事件（analyst1）=="
INC=$(req POST /incidents "$ANALYST" '{"title":"VPN 异常登录","description":"多地同时登录","severity":"P1"}' | python3 -c 'import sys,json;print(json.load(sys.stdin)["id"])')
echo "incident=$INC"

echo "== 2. P1 未分配响应人员，分诊被门禁拦截（预期 422 triage_gate）=="
req POST "/incidents/$INC/transitions" "$ANALYST" '{"toStatus":"triaged","expectedVersion":1,"requestId":"demo-triage-1"}'

echo "== 3. admin 分配 responder1 =="
req POST "/incidents/$INC/assign" "$ADMIN" "{\"responderId\":\"$RESPONDER\"}"

echo "== 4. 分诊（注意版本已变为 2）；重复 requestId 演示幂等 =="
req POST "/incidents/$INC/transitions" "$ANALYST" '{"toStatus":"triaged","expectedVersion":2,"requestId":"demo-triage-2"}'
req POST "/incidents/$INC/transitions" "$ANALYST" '{"toStatus":"triaged","expectedVersion":2,"requestId":"demo-triage-2"}'

echo "== 5. 提交证据与纠正说明 =="
EV=$(req POST "/incidents/$INC/evidence" "$ANALYST" '{"content":"认证日志：02:13 来自两个不同国家的成功登录"}' | python3 -c 'import sys,json;print(json.load(sys.stdin)["id"])')
req POST "/evidence/$EV/notes" "$ANALYST" '{"content":"纠正：时区为 UTC+8，实际为 18:13 UTC"}'

echo "== 6. 处置阶段：遏制→根除→恢复→复盘（responder1，版本 3..6）=="
req POST "/incidents/$INC/transitions" "$RESPONDER" '{"toStatus":"contained","expectedVersion":3,"requestId":"demo-contain","note":"禁用受影响账号"}'
req POST "/incidents/$INC/transitions" "$RESPONDER" '{"toStatus":"eradicated","expectedVersion":4,"requestId":"demo-eradicate","note":"吊销会话并重置凭据"}'
req POST "/incidents/$INC/transitions" "$RESPONDER" '{"toStatus":"recovered","expectedVersion":5,"requestId":"demo-recover","note":"账号恢复并启用 MFA"}'
req POST "/incidents/$INC/transitions" "$RESPONDER" '{"toStatus":"postmortem","expectedVersion":6,"requestId":"demo-postmortem"}'

echo "== 7. 关闭门槛：缺少根因/行动项时被拦截（预期 422 close_gate）=="
req POST "/incidents/$INC/transitions" "$RESPONDER" '{"toStatus":"closed","expectedVersion":7,"requestId":"demo-close-1"}'

echo "== 8. 补齐复盘字段与行动项后关闭 =="
req PUT "/incidents/$INC/postmortem" "$RESPONDER" '{"rootCause":"MFA 未覆盖 VPN 旧客户端","lessonsLearned":"遗留客户端需纳入统一 MFA 基线"}'
req POST "/incidents/$INC/action-items" "$RESPONDER" "{\"title\":\"强制旧客户端升级或阻断\",\"ownerId\":\"$RESPONDER\",\"dueAt\":\"$(date -u -d '+3 days' +%Y-%m-%dT%H:%M:%SZ)\"}"
req POST "/incidents/$INC/transitions" "$RESPONDER" '{"toStatus":"closed","expectedVersion":7,"requestId":"demo-close-2"}'

echo "== 9. 导出报告（阶段记录 + 证据摘要 + 指标）=="
req GET "/incidents/$INC/export" "$ANALYST"
