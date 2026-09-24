# HTTP 请求样例

以下样例假设 demo 发行方 (`:8787`) 与网关 (`:8080`) 均已在本机启动
（启动方式见 README）。也可以直接运行 `python scripts/demo_requests.py`
自动跑完这些场景。

## 1. 健康检查 / 受信发行方清单

```bash
curl -s http://127.0.0.1:8080/health
# {"status":"ok","issuers_configured":3}

curl -s http://127.0.0.1:8080/issuers
```

## 2. 向 demo 发行方申请一个 RS256 令牌并验证

```bash
TOKEN=$(curl -s -X POST http://127.0.0.1:8787/token \
  -H 'Content-Type: application/json' \
  -d '{"issuer":"rsa","alg":"RS256","aud":"api-gateway","lifetime":300}' \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["token"])')

curl -s -X POST http://127.0.0.1:8080/verify \
  -H 'Content-Type: application/json' \
  -d "{\"token\":\"$TOKEN\"}"
```

成功响应 `200`：

```json
{
  "valid": true,
  "issuer_id": "demo-rsa",
  "alg": "RS256",
  "kid": "rsa-1",
  "claims": { "iss": "...", "sub": "...", "aud": "...", "exp": 1790252341 }
}
```

其他算法：把 `"issuer":"ec","alg":"EdDSA"`（或 ES256/384/512）、
`"issuer":"hmac","alg":"HS256"` 换入即可。

## 3. 调用方额外要求 aud

```bash
curl -s -X POST http://127.0.0.1:8080/verify \
  -H 'Content-Type: application/json' \
  -d "{\"token\":\"$TOKEN\",\"expected_aud\":\"api-gateway\"}"
```

## 4. 拒绝响应的统一结构

任何拒绝都返回 `400`（令牌结构问题）或 `401`（信任/时间/签名问题），
body 形如：

```json
{
  "error": "alg_not_allowed",
  "reason": "算法 'HS256' 不在发行方 'demo-rsa' 的显式允许列表中",
  "context": {
    "alg": "HS256",
    "issuer_id": "demo-rsa",
    "allowed_algs": ["PS256", "RS256", "RS384", "RS512"]
  },
  "denied": true
}
```

`error` 是稳定机器码（见 `app/errors.py` 的 `ErrCode`），`context` 不含
令牌原文。典型码：

| error                         | 含义                                   | HTTP |
|-------------------------------|----------------------------------------|------|
| `malformed_token`             | 不是 3 段紧凑 JWT                      | 400  |
| `bad_base64url` / `bad_json`  | 编码/JSON 非法                         | 400  |
| `duplicate_json_key`          | 头或载荷含重复 JSON 键                 | 400  |
| `alg_not_allowed`             | 算法不在该发行方显式白名单（含 `none`）| 401  |
| `header_parameter_forbidden`  | 头含 `jku`/`jwk`/`x5u`/`x5c` 等        | 400  |
| `unrecognized_crit`           | `crit` 含验证方不理解的扩展            | 400  |
| `unknown_issuer`              | `iss` 不在服务端受信列表               | 401  |
| `kid_not_found`               | 该发行方 JWKS 中无此 kid               | 401  |
| `duplicate_kid_in_jwks`       | 远程 JWKS 文档内 kid 重复（整份拒绝）  | 401  |
| `jwks_fetch_failed`           | JWKS 不可达且无缓存可用                | 401  |
| `key_alg_mismatch`            | 密钥族/曲线/JWK alg 与令牌头不一致     | 401  |
| `invalid_signature`           | 签名不通过                             | 401  |
| `iss_mismatch`               | iss 与取钥发行方不一致（保留码）       | 401  |
| `aud_not_allowed`             | aud 无交集                             | 401  |
| `token_expired`               | `now >= exp+leeway`                    | 401  |
| `token_not_yet_valid`         | `now+leeway < nbf`（或 iat 在未来）    | 401  |

## 5. 攻击复现：RS256 → HS256 算法混淆

```bash
# 拿到发行方公开的 RSA 公钥（JWKS 本来就是公开的），用它当 HMAC 密钥签 HS256
python3 - <<'PY'
import base64, hashlib, hmac, json, time, urllib.request
import httpx
from app.jwks import jwk_to_public_key   # PYTHONPATH=.
from cryptography.hazmat.primitives import serialization
from app.jwt_sign import b64u_json

jwks = httpx.get("http://127.0.0.1:8787/rsa/jwks.json").json()
pem, _ = jwk_to_public_key(jwks["keys"][0])
pub_pem = pem.public_bytes(
    serialization.Encoding.PEM,
    serialization.PublicFormat.SubjectPublicKeyInfo,
)
now = int(time.time())
h = b64u_json({"alg": "HS256", "typ": "JWT", "kid": "rsa-1"})
p = b64u_json({"iss": "https://demo-issuer.local/rsa",
               "sub": "attacker", "aud": "api-gateway",
               "iat": now, "exp": now + 300})
sig = hmac.new(pub_pem, f"{h}.{p}".encode(), hashlib.sha256).digest()
token = f"{h}.{p}.{base64.urlsafe_b64encode(sig).rstrip(b'=').decode()}"
print(httpx.post("http://127.0.0.1:8080/verify",
                 json={"token": token}).json())
# -> 401 {"error":"alg_not_allowed", ...}
PY
```

## 6. 攻击复现：jku 指定攻击者取钥地址

```bash
GOOD=$(curl -s -X POST http://127.0.0.1:8787/token -H 'Content-Type: application/json' \
  -d '{"issuer":"rsa","alg":"RS256"}' | python3 -c 'import sys,json;print(json.load(sys.stdin)["token"])')

python3 - <<PY
from app.jwt_sign import b64u_json   # PYTHONPATH=.
good = "$GOOD"
h, p, s = good.split(".")
evil = b64u_json({"alg":"RS256","typ":"JWT","kid":"rsa-1",
                  "jku":"https://attacker.example/evil.json"})
import httpx
print(httpx.post("http://127.0.0.1:8080/verify",
                 json={"token": f"{evil}.{p}.{s}"}).json())
# -> 400 {"error":"header_parameter_forbidden","context":{"parameter":"jku"}}
PY
```

## 7. 密钥轮换与缓存

```bash
# 发行方轮换密钥
curl -s -X POST http://127.0.0.1:8787/rotate

# 网关立即强制刷新某发行方缓存（或等待 cache_ttl 自然过期）
curl -s -X POST http://127.0.0.1:8080/admin/issuers/demo-rsa/refresh
# {"refreshed":true,"issuer_id":"demo-rsa","keys":1}

# 查看每个发行方的缓存（kid 列表、年龄、TTL、是否新鲜）
curl -s http://127.0.0.1:8080/admin/issuers
```

## 8. 边界时刻速查（leeway = 0）

| 条件（秒）           | 结果        | error                 |
|----------------------|-------------|------------------------|
| `now == exp`         | 拒绝        | `token_expired`        |
| `now == exp - 1`     | 通过        | —                      |
| `now == nbf`         | 通过（恰好生效）| —                  |
| `now == nbf - 1`     | 拒绝        | `token_not_yet_valid`  |

`leeway` 向宽容方向平移：过期 `leeway` 秒**整**仍算过期
（`now < exp + leeway` 才通过），`nbf` 同理。
