#!/usr/bin/env bash
# 端到端演示（curl）：启动服务 -> 拆分 -> 多种恢复/拒绝场景 -> 关闭服务。
# 用法：bash examples/http_demo.sh [端口]
set -euo pipefail

PORT="${1:-0}"
if [ "${PORT}" = "0" ]; then
  # 让内核分配一个空闲端口
  PORT=$(python3 -c 'import socket;s=socket.socket();s.bind(("127.0.0.1",0));print(s.getsockname()[1]);s.close()')
fi
BASE="http://127.0.0.1:${PORT}"
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "${HERE}/.." && pwd)"

cd "${ROOT}"
python3 -m tss.cli serve --host 127.0.0.1 --port "${PORT}" >/tmp/tss_demo_server.log 2>&1 &
SERVER_PID=$!
trap 'kill ${SERVER_PID} 2>/dev/null || true' EXIT

# 等待本服务就绪：校验响应体确实是本服务（避免端口被其他进程占用时误判）
READY=0
for _ in $(seq 1 50); do
  if ! kill -0 "${SERVER_PID}" 2>/dev/null; then
    echo "服务进程启动失败，日志：" >&2
    cat /tmp/tss_demo_server.log >&2
    exit 1
  fi
  BODY=$(curl -s "${BASE}/healthz" 2>/dev/null || true)
  if echo "${BODY}" | grep -q '"service": "tss-local"'; then READY=1; break; fi
  sleep 0.1
done
[ "${READY}" = "1" ] || { echo "服务未在预期时间内就绪" >&2; exit 1; }

echo "=== 1) healthz ==="
curl -s "${BASE}/healthz"; echo

echo "=== 2) split: 3-of-5（中文文本秘密）==="
curl -s -X POST "${BASE}/split" \
  -H 'Content-Type: application/json' \
  -d @examples/split_request.json | python3 -m json.tool

# 全部交互用一个内联 Python 助手完成（份额是随机的，需要动态拼装请求）
PORT="${PORT}" python3 - <<'PY'
import base64, hashlib, json, os, urllib.request, urllib.error

BASE = f"http://127.0.0.1:{os.environ['PORT']}"

def call(path, payload):
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())

def show(title, status, body):
    print(f"\n=== {title} [HTTP {status}] ===")
    print(json.dumps(body, ensure_ascii=False, indent=2))

# 3-of-5 拆分
_, split = call("/split", {"secret_text": "这是一个需要被阈值分享保护的秘密",
                           "threshold": 3, "total": 5})
shares = split["shares"]
print(f"split_id={split['split_id']}，得到 {len(shares)} 个份额")

# 恰好阈值、非连续子集
show("3) recover 恰好 3 个份额（第 1/3/5 个，验证任意阈值子集）",
     *call("/recover", {"shares": [shares[0], shares[2], shares[4]]}))

# 超过阈值
show("4) recover 给全部 5 个（超阈值 -> 子集一致性诊断）",
     *call("/recover", {"shares": shares}))

# 少于阈值
show("5) 只有 2 个份额（< 阈值）-> 拒绝 below_threshold",
     *call("/recover", {"shares": shares[:2]}))

# 重复横坐标
show("6) 同一个份额出现两次 -> 拒绝 duplicate_index",
     *call("/recover", {"shares": [shares[0], shares[0], shares[1]]}))

# 损坏编码混入
show("7) 1 个坏 base64 + 3 个好份额（t=3）-> 隔离坏份额，正常恢复",
     *call("/recover", {"shares": ["@@@not-base64@@@", shares[1], shares[2], shares[3]]}))

# 校验和损坏混入
raw = bytearray(base64.b64decode(shares[0]))
raw[30] ^= 0x01
flipped = base64.b64encode(bytes(raw)).decode()
show("8) 1 个载荷翻位（SHA256 失配）+ 3 个好份额 -> 隔离后恢复，rejected 有记录",
     *call("/recover", {"shares": [flipped, shares[1], shares[2], shares[3]]}))

# 混批
_, other = call("/split", {"secret_text": "另一批秘密", "threshold": 3, "total": 5})
show("9) 两批不同 split 的份额混在一起 -> 拒绝 inconsistent_shares",
     *call("/recover", {"shares": [shares[0], shares[1], other["shares"][2]]}))

# 重算校验和的恶意伪造（无 HMAC）：n=t+1 能检出，不能定位
_, split4 = call("/split", {"secret_text": "forged-secret", "threshold": 3, "total": 4})
s4 = split4["shares"]
raw = bytearray(base64.b64decode(s4[3]))
raw[30] ^= 0xAA
raw[-32:] = hashlib.sha256(bytes(raw[:-32])).digest()
forged = base64.b64encode(bytes(raw)).decode()
show("10) 恶意份额（翻位并重算 SHA256，绕过校验和）-> 拒绝恢复；"
     "注意 n=t+1 时无法可靠指认坏份额",
     *call("/recover", {"shares": [s4[0], s4[1], s4[2], forged]}))

# HMAC 认证模式
key = base64.b64encode(os.urandom(32)).decode()
_, auth_split = call("/split", {"secret_text": "认证模式秘密", "threshold": 3,
                                "total": 5, "auth_key_b64": key})
show("11) HMAC 模式拆分（authenticated=true）", 200,
     {"split_id": auth_split["split_id"], "authenticated": auth_split["authenticated"]})
show("12) HMAC 模式正常恢复",
     *call("/recover", {"shares": auth_split["shares"][:3], "auth_key_b64": key}))
raw = bytearray(base64.b64decode(auth_split["shares"][0]))
raw[30] ^= 0x55
raw[-32:] = hashlib.sha256(bytes(raw[:-32])).digest()
forged_mac = base64.b64encode(bytes(raw)).decode()
show("13) HMAC 模式下伪造并重算 SHA256 -> 标签层直接拒绝，可用份额不足 -> 拒绝恢复",
     *call("/recover", {"shares": [forged_mac] + auth_split["shares"][1:3],
                        "auth_key_b64": key}))
show("14) validate 单个份额",
     *call("/validate", {"share": shares[0]}))
PY

echo
echo "=== demo 完成，服务日志见 /tmp/tss_demo_server.log ==="
