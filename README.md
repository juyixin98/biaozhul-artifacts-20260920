# JWT 多发行方验证网关

纯后端服务（FastAPI + `cryptography`，**不使用任何 JWT 库**），对多个显式配置的发行方验证 JWT，重点防御算法混淆与令牌指定取钥地址类攻击，并覆盖密钥轮换、重复 kid 与时间边界。

## 1. 环境与启动

要求：Python 3.12（3.10+ 亦可），可访问 PyPI。

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt     # 测试依赖，含运行时依赖
# 仅运行时：.venv/bin/pip install -r requirements.txt
```

依赖版本锁定见 `requirements.lock`（`pip freeze` 实测生成）。

启动网关（默认读 `config/issuers.json`，可用 `JWT_GATEWAY_CONFIG` 覆盖）：

```bash
JWT_GATEWAY_CONFIG=config/issuers.json \
  .venv/bin/python -m uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8080
```

接口文档：启动后访问 `http://127.0.0.1:8080/docs`。

一键本地演示（另起一个模拟 IdP，跑完自动退出）：

```bash
bash scripts/run_demo.sh
```

## 2. 配置

配置是本地 JSON，**发行方、JWKS 地址、audience、算法全部来自配置，请求无法影响**。

```json
{
  "issuers": [
    {
      "issuer_id": "example-prod",
      "iss": "https://idp.example.com/",
      "jwks_uri": "https://idp.example.com/.well-known/jwks.json",
      "audience": "https://api.example.com",
      "algorithms": ["RS256", "ES256"],
      "cache_ttl_seconds": 600,
      "negative_cooldown_seconds": 30,
      "leeway_seconds": 30
    }
  ]
}
```

| 字段 | 说明 |
|---|---|
| `issuer_id` | 网关内部标识；请求可用 `issuer_id` 显式指定发行方 |
| `iss` | 要求令牌 `iss` 声明精确等于的值 |
| `jwks_uri` | **唯一**取钥地址；必须 https；本地回环 http 需显式 `allow_http: true` |
| `audience` | 令牌 `aud` 必须包含的值 |
| `algorithms` | 显式允许的签名算法。当前支持 `RS256/RS384/RS512`、`ES256/ES384`；没有 HMAC，也没有 `none` |
| `cache_ttl_seconds` | JWKS 正常缓存时长 |
| `negative_cooldown_seconds` | 未知 kid / 取钥失败后的最小重试间隔（防取钥风暴） |
| `leeway_seconds` | exp/nbf/iat 时钟偏差宽限 |

## 3. HTTP 接口与请求样例

### `POST /v1/verify`

两种传令牌方式：JSON body 或 `Authorization` 头。

```bash
curl -sS -X POST http://127.0.0.1:8080/v1/verify \
  -H 'content-type: application/json' \
  -d '{"token": "<JWT>"}'

# 或显式钉住发行方
curl -sS -X POST http://127.0.0.1:8080/v1/verify \
  -H 'content-type: application/json' \
  -d '{"token": "<JWT>", "issuer_id": "demo-rsa"}'

# 或
curl -sS -X POST http://127.0.0.1:8080/v1/verify \
  -H 'Authorization: Bearer <JWT>'
```

成功：`200`

```json
{
  "status": "ok",
  "issuer_id": "demo-rsa",
  "iss": "https://demo-idp.local/rsa",
  "kid": "rsa-k1",
  "alg": "RS256",
  "claims": { "sub": "demo-user-42", "aud": "gateway-demo", "...": "..." }
}
```

失败：`401`（结构/请求类问题为 `400`），统一结构化信封，并带
`WWW-Authenticate: Bearer error="<code>"`：

```json
{
  "status": "error",
  "error": {
    "code": "algorithm_not_allowed",
    "message": "token algorithm is not in the issuer's explicit allow-list",
    "details": { "alg": "HS256", "allowed_algorithms": ["RS256"] }
  }
}
```

### 其他接口

| 方法 路径 | 作用 |
|---|---|
| `GET /healthz` | 存活检查 |
| `GET /v1/issuers` | 列出发行方、audience、允许算法（不暴露 jwks_uri） |
| `GET /admin/issuers/{id}/cache` | 查看该发行方 JWKS 缓存（kid、alg、到期时间、上次错误） |
| `POST /admin/issuers/{id}/refresh` | 强制刷新（轮换即时生效，不必等 TTL） |

## 4. 拒绝原因（结构化错误码）

| code | 触发条件 |
|---|---|
| `missing_token` / `invalid_request` | 未提供令牌或请求格式错误 |
| `malformed_token` / `malformed_header` / `malformed_payload` | 分段数、base64url 或 JSON 结构非法 |
| `empty_signature` | 签名段为空 |
| `missing_issuer` / `unknown_issuer` | 无 `iss` 或发行方不在本地白名单 |
| `invalid_header` / `missing_kid` | 头部字段非法或缺 kid |
| `algorithm_not_allowed` | `alg` 不在该发行方允许列表（含 `none`、`HS*`、跨发行方算法） |
| `header_key_reference_forbidden` | 头部出现 `jku`/`jwk`/`x5u`/`x5c`/`x5t` |
| `critical_header_unsupported` | 出现 `crit` 扩展 |
| `duplicate_kid` | JWKS 中可用键 kid 重复（键选择有歧义，整份文档拒绝） |
| `unknown_kid` | JWKS 中无该 kid |
| `key_alg_mismatch` / `key_type_mismatch` | JWK 的 alg/类型与令牌不一致 |
| `invalid_signature` | 签名验不过（含攻击者同 kid 伪造键） |
| `issuer_mismatch` | 签名后复核 `iss` 与配置不一致 |
| `invalid_audience` | `aud` 不含期望值 |
| `token_expired` / `token_not_yet_valid` / `issued_in_future` | exp/nbf/iat 时间检查失败 |
| `invalid_claim` | 时间声明类型非法等 |
| `jwks_unavailable` | JWKS 取钥失败（冷缓存）；取钥失败不清空旧好键 |

## 5. 安全设计要点

1. **算法混淆**：验签分发只认“发行方配置允许”的算法；允许集合里根本没有 HMAC，因此
   RS256→HS256 攻击在取钥之前即被拒。此外校验 JWK 类型与算法族一致
   （RSA 键不能用于 ES*，反之亦然），RSA 模长 <2048 拒绝。
2. **取钥地址不可由令牌指定**：`jku`、`jwk`、`x5u`、`x5c`、`x5t#S256` 头部一律拒绝；
   HTTP 取钥器禁止重定向、GET only、5s 超时、64KB 上限、校验 content-type。
3. **按发行方隔离缓存**：每个发行方独立 `IssuerCache`，kid 相同也互不串用；
   未知 kid 受负缓存冷却限制，防止被恶意令牌打成取钥放大攻击。
4. **重复 kid**：JWKS 中可被选中的键若 kid 重复，直接拒绝整份文档（`duplicate_kid`），
   而不是“任选其一”。
5. **轮换**：TTL 到期刷新；未知 kid 触发一次刷新；管理端点可强制刷新；
   刷新失败保留上一份好键，发行方短暂宕机不连累已签发令牌。
6. **时间边界**：`exp == now` 仍有效，`now > exp + leeway` 才过期；
   `nbf == now` 有效；宽限按发行方配置。
7. **日志不泄密**：结构化 JSON 日志只记录事件、发行方、kid、alg、错误码和
   令牌 SHA-256 前 16 位指纹；完整令牌、签名、声明值绝不落日志；
   响应体内也不含令牌；异常处理器不回传内部错误文本。

## 6. 测试

```bash
.venv/bin/pytest -q
```

覆盖（`tests/`）：

- 算法混淆：`none`、RS256→HS256（公钥当 HMAC 密钥）、ES256 令牌发给 RS256 发行方；
- 头部注入：`jku`/`x5u`/`jwk`/`crit`、缺 kid；
- 重复 kid：整份 JWKS 拒绝；不允许算法造成的“假重复”不误报；
- 轮换与缓存：未知 kid 恰好刷新一次、负缓存冷却、TTL 边界、管理端强制刷新、
  刷新失败保留旧键、跨发行方缓存隔离、相同 kid 跨发行方不串用；
- 边界时刻：exp/nbf 精确相等、±1 秒、leeway、未来 iat、非数字时间声明；
- JWKS 解析：对称键拒绝、弱 RSA、alg/键类型不符、非 JSON、非有限数；
- HTTP 取钥器：重定向拒绝、非 200、超长、错误 content-type、网络错误；
- API：两种传令牌方式、结构化错误信封、管理端点；
- 日志脱敏：构造带金丝雀字符串的攻击令牌，断言日志中无令牌/金丝雀，仅有指纹。

实际运行结果记录在 `RESULTS.md`。

## 7. 目录结构

```
app/
  main.py          FastAPI 应用工厂与 HTTP 接口
  config.py        本地发行方配置加载
  logging.py       JSON 审计日志（令牌指纹，不含原文）
  jwtv/
    verifier.py    验证管线（结构→头部策略→发行方路由→验签→声明）
    jwks.py        IssuerConfig / IssuerCache / IssuerRegistry / HTTP 取钥器
    jws.py         RS*/ES* 验签与签名（cryptography）
    jwk.py         JWK→公钥解析（仅 RSA/EC，最小模长/曲线检查）
    claims.py      iss/aud/exp/nbf/iat 校验（可注入时钟与 leeway）
    b64.py         base64url
    errors.py      TokenError 结构化错误
config/issuers.json          本地演示配置（allow_http 指向 127.0.0.1）
config/issuers.example.json  生产配置模板
scripts/demo_idp.py          模拟 IdP（JWKS、签发各类攻击令牌、轮换）
scripts/run_demo.sh          一键演示
tests/                       pytest 测试
```

## 8. 已知限制（如实声明）

- 未实现分布式多实例缓存共享；每实例独立缓存（对正确性无影响，仅影响取钥次数）。
- 未做管理端点鉴权（`/admin/*`），生产部署应放在内网或加网络层鉴权。
- JWKS 端点未做 TLS 证书钉扎（使用系统信任库）；也未做私网地址 SSRF 过滤，
  生产环境 jwks_uri 由本地配置给出、且只允许 https，风险面已很小，但可再加。
- 未支持 RS-PSS 与 EdDSA、未支持 `x5c` 证书链校验。
