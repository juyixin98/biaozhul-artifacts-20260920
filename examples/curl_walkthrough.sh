#!/usr/bin/env bash
# 纯 HTTP（curl）完整走查：初始化根 -> 签名登记 -> 验签 -> 篡改 -> 回退 ->
# 阈值轮换 -> 旧密钥失效。密钥与请求体均为测试用途，服务需先启动：
#   STATE_PATH=data/curl-state.json uvicorn app.main:app --port 8000
set -euo pipefail
cd "$(dirname "$0")/.."
BASE=${BASE:-http://127.0.0.1:8000}
REQ=data/req
mkdir -p "$REQ"

echo '### 0) 健康检查'
curl -s "$BASE/healthz"; echo

echo
echo '### 1) 初始化信任根（threshold=2/3 阈值成员 + 2 个制品签名者）'
python3 - "$REQ/init.json" <<'PY'
import json, os, sys
out = sys.argv[1]
d = "dev-keys"
root = json.load(open(os.path.join(d, "root_threshold_keys.json")))
signer = json.load(open(os.path.join(d, "signer_keys.json")))
json.dump({
    "root_version": 1,
    "threshold": 2,
    "threshold_public_keys": [k["public_key_hex"] for k in root],
    "signer_public_keys": [k["public_key_hex"] for k in signer],
}, open(out, "w"))
PY
curl -s -X POST "$BASE/root/init" -H 'Content-Type: application/json' \
  -d @"$REQ/init.json"; echo

echo
echo '### 2) 客户端离线签名并登记 firmware 1.0.0'
python3 - "$REQ/sign-1.0.0.json" <<'PY'
import json, os, sys
sys.path.insert(0, ".")
from app import crypto
out = sys.argv[1]
signer = json.load(open("dev-keys/signer_keys.json"))[0]
content = b"firmware-release-1.0.0 bytes"
digest = crypto.sha256_hex(content)
nonce = crypto.generate_nonce()
sig = crypto.sign_artifact(
    private_key_hex=signer["private_key_hex"],
    digest_hex=digest, artifact_type="firmware",
    version="1.0.0", nonce_hex=nonce,
)
body = {
    "artifact_type": "firmware", "version": "1.0.0",
    "digest": digest, "key_id": signer["key_id"],
    "nonce": nonce, "signature": sig,
}
json.dump(body, open(out, "w"))
print(f"# 正文={content!r} 摘要={digest}")
PY
curl -s -X POST "$BASE/artifacts/sign" -H 'Content-Type: application/json' \
  -d @"$REQ/sign-1.0.0.json"; echo

echo
echo '### 3) 验签：用登记时的真实摘要'
python3 - "$REQ/verify-ok.json" "$REQ/sign-1.0.0.json" <<'PY'
import json, sys
b = json.load(open(sys.argv[2]))
json.dump({k: b[k] for k in ("artifact_type", "version", "digest", "nonce", "signature")},
          open(sys.argv[1], "w"))
PY
curl -s -X POST "$BASE/artifacts/verify" -H 'Content-Type: application/json' \
  -d @"$REQ/verify-ok.json"; echo

echo
echo '### 4) 验签：正文被篡改（摘要换成另一段内容）'
python3 - "$REQ/verify-tampered.json" "$REQ/sign-1.0.0.json" <<'PY'
import json, sys
sys.path.insert(0, ".")
from app import crypto
b = json.load(open(sys.argv[2]))
b["digest"] = crypto.sha256_hex(b"firmware-release-1.0.0 TAMPERED")
json.dump(b, open(sys.argv[1], "w"))
PY
curl -s -X POST "$BASE/artifacts/verify" -H 'Content-Type: application/json' \
  -d @"$REQ/verify-tampered.json"; echo

echo
echo '### 5) 重复登记同一份签名（应 409）'
curl -s -o /dev/stderr -w 'HTTP %{http_code}\n' -X POST "$BASE/artifacts/sign" \
  -H 'Content-Type: application/json' -d @"$REQ/sign-1.0.0.json"

echo
echo '### 6) 版本回退：先登记 2.0.0，再尝试 1.5.0（后者应 409）'
python3 - "$REQ/sign-2.0.0.json" <<'PY'
import json, sys
sys.path.insert(0, ".")
from app import crypto
out = sys.argv[1]
signer = json.load(open("dev-keys/signer_keys.json"))[0]
content = b"firmware-release-2.0.0 bytes"
digest = crypto.sha256_hex(content); nonce = crypto.generate_nonce()
sig = crypto.sign_artifact(
    private_key_hex=signer["private_key_hex"], digest_hex=digest,
    artifact_type="firmware", version="2.0.0", nonce_hex=nonce,
)
json.dump({"artifact_type": "firmware", "version": "2.0.0", "digest": digest,
           "key_id": signer["key_id"], "nonce": nonce, "signature": sig}, open(out, "w"))
PY
curl -s -o /dev/null -X POST "$BASE/artifacts/sign" -H 'Content-Type: application/json' \
  -d @"$REQ/sign-2.0.0.json"
echo "2.0.0 登记完成"
python3 - "$REQ/sign-1.5.0.json" <<'PY'
import json, sys
sys.path.insert(0, ".")
from app import crypto
out = sys.argv[1]
signer = json.load(open("dev-keys/signer_keys.json"))[0]
content = b"firmware-release-1.5.0 bytes"
digest = crypto.sha256_hex(content); nonce = crypto.generate_nonce()
sig = crypto.sign_artifact(
    private_key_hex=signer["private_key_hex"], digest_hex=digest,
    artifact_type="firmware", version="1.5.0", nonce_hex=nonce,
)
json.dump({"artifact_type": "firmware", "version": "1.5.0", "digest": digest,
           "key_id": signer["key_id"], "nonce": nonce, "signature": sig}, open(out, "w"))
PY
curl -s -o /dev/stderr -w 'HTTP %{http_code} ' -X POST "$BASE/artifacts/sign" \
  -H 'Content-Type: application/json' -d @"$REQ/sign-1.5.0.json"; echo

echo
echo '### 7) 根轮换：只有 1 个旧根批准（应 403）'
python3 - "$REQ/rotate-1of2.json" <<'PY'
import json, sys
sys.path.insert(0, ".")
from app import crypto
out = sys.argv[1]
root = json.load(open("dev-keys/root_threshold_keys.json"))
new_root = json.load(open("dev-keys/new_threshold_keys.json"))
new_signer = json.load(open("dev-keys/new_signer_keys.json"))
new_t_pubs = [k["public_key_hex"] for k in new_root]
new_s_pubs = [k["public_key_hex"] for k in new_signer]
def approval(k):
    return {"key_id": k["key_id"], "signature": crypto.sign_root_rotation(
        private_key_hex=k["private_key_hex"], new_root_version=2, new_threshold=2,
        new_threshold_keys=new_t_pubs, new_signer_keys=new_s_pubs)}
json.dump({"new_root_version": 2, "new_threshold": 2,
           "new_threshold_public_keys": new_t_pubs,
           "new_signer_public_keys": new_s_pubs,
           "approvals": [approval(root[0])]}, open(out, "w"))
PY
curl -s -o /dev/stderr -w 'HTTP %{http_code}\n' -X POST "$BASE/root/rotate" \
  -H 'Content-Type: application/json' -d @"$REQ/rotate-1of2.json"

echo
echo '### 8) 根轮换：2 个旧根成员批准（应 200，root_version=2）'
python3 - "$REQ/rotate-2of2.json" <<'PY'
import json, sys
sys.path.insert(0, ".")
from app import crypto
out = sys.argv[1]
root = json.load(open("dev-keys/root_threshold_keys.json"))
new_root = json.load(open("dev-keys/new_threshold_keys.json"))
new_signer = json.load(open("dev-keys/new_signer_keys.json"))
new_t_pubs = [k["public_key_hex"] for k in new_root]
new_s_pubs = [k["public_key_hex"] for k in new_signer]
def approval(k):
    return {"key_id": k["key_id"], "signature": crypto.sign_root_rotation(
        private_key_hex=k["private_key_hex"], new_root_version=2, new_threshold=2,
        new_threshold_keys=new_t_pubs, new_signer_keys=new_s_pubs)}
json.dump({"new_root_version": 2, "new_threshold": 2,
           "new_threshold_public_keys": new_t_pubs,
           "new_signer_public_keys": new_s_pubs,
           "approvals": [approval(root[0]), approval(root[1])]}, open(out, "w"))
PY
curl -s -X POST "$BASE/root/rotate" -H 'Content-Type: application/json' \
  -d @"$REQ/rotate-2of2.json"; echo

echo
echo '### 9) 轮换后用旧签名者登记新制品（应 403，密钥已失效）'
python3 - "$REQ/sign-old-signer.json" <<'PY'
import json, sys
sys.path.insert(0, ".")
from app import crypto
out = sys.argv[1]
signer = json.load(open("dev-keys/signer_keys.json"))[0]
digest = crypto.sha256_hex(b"firmware-3.0.0 by old signer"); nonce = crypto.generate_nonce()
sig = crypto.sign_artifact(
    private_key_hex=signer["private_key_hex"], digest_hex=digest,
    artifact_type="firmware", version="3.0.0", nonce_hex=nonce,
)
json.dump({"artifact_type": "firmware", "version": "3.0.0", "digest": digest,
           "key_id": signer["key_id"], "nonce": nonce, "signature": sig}, open(out, "w"))
PY
curl -s -o /dev/stderr -w 'HTTP %{http_code}\n' -X POST "$BASE/artifacts/sign" \
  -H 'Content-Type: application/json' -d @"$REQ/sign-old-signer.json"
