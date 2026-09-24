# JWT 多发行方验证网关（纯后端）

基于 **Python 3.12 + FastAPI + cryptography** 实现的 JWT/JWS 验证网关。
不依赖 `pyjwt`：所有验签逻辑直接用 `cryptography` 原语完成，安全决策
（算法、取钥地址、时间边界）全部显式可控。

核心安全目标：

1. **显式算法白名单**：每个发行方独立配置允许的签名算法，未列出的算法
   （含 `none`）一律拒绝；对称/非对称密钥族分开，杜绝 RS256→HS256 混淆。
2. **按发行方隔离的 JWKS 缓存**：令牌的 `iss` 只用于索引**服务端配置**；
   不同发行方缓存、锁、TTL 完全独立，相同 `kid` 不会串台。
3. **禁止令牌指定取钥地址**：头中的 `jku` / `jwk` / `x5u` / `x5c` /
   `x5t` / `x5t#S256` 一律拒绝，永远只请求配置中的 JWKS URI。
4. **完整声明校验**：`aud`、`exp`（必填）、`nbf`（选填）、`iat`、
   强制 `kid`，时间边界按 RFC 7519 严格处理并支持配置 leeway。
5. **结构化拒绝原因**：稳定错误码 + 中文说明 + 机器可读 `context`。
6. **日志不泄露令牌**：成功/失败日志只记 `issuer_id / kid / alg /
   错误码 / SHA-256 短指纹(48bit)`，绝不记录令牌原文或任一段。

支持算法：`RS256/384/512`、`PS256/384/512`、`ES256/384/512`、
`EdDSA (Ed25519)`、`HS256/384/512`。

---

## 1. 目录结构

```
.
├── app/
│   ├── config.py       # 发行方配置（信任根）：加载、校验、JWKS URI 安全检查
│   ├── errors.py       # 稳定错误码 ErrCode 与 VerifyError
│   ├── jwks.py         # JWK 解析、urllib 抓取器、按发行方隔离缓存
│   ├── verifier.py     # JWT 验证核心（结构→算法→取钥→验签→声明）
│   ├── main.py         # FastAPI 应用与 HTTP 接口、日志脱敏
│   └── jwt_sign.py     # 签发工具（仅测试与 demo 使用，非生产路径）
├── config/
│   └── issuers.json    # 3 个 demo 发行方配置
├── scripts/
│   ├── demo_issuer.py  # 本地 demo 发行方（JWKS + 现签令牌 + 轮换）
│   └── demo_requests.py# 11 个端到端场景脚本
├── examples/
│   └── HTTP_API.md     # 全部 HTTP 接口与 curl/攻击复现样例
├── tests/              # 80 个 pytest 测试（含真实 HTTP 端到端）
├── requirements.txt
└── requirements-lock.txt
```

## 2. 安装与启动

需要 Python 3.10+（开发与实测在 **Python 3.12.3 / Linux**）。

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-lock.txt   # 或 requirements.txt
```

启动 **demo 发行方**（终端 1，监听 127.0.0.1:8787）：

```bash
PYTHONPATH=. python scripts/demo_issuer.py
```

启动 **网关**（终端 2，监听 127.0.0.1:8080；端口被占用时换成例如 8090，
演示脚本可用 `GATEWAY_URL=http://127.0.0.1:8090` 覆盖）：

```bash
JWT_GATEWAY_CONFIG=config/issuers.json \
PYTHONPATH=. python -m uvicorn app.main:app --host 127.0.0.1 --port 8080
```

生产环境把 JWKS URI 配成 `https://` 真实地址即可；配置加载时仅放行
`https`，明文 `http` 只允许环回地址（localhost/127.0.0.1/::1）。

## 3. 运行测试

```bash
source .venv/bin/activate
python -m pytest                 # 80 passed
python -m pytest -v              # 查看逐条用例
```

端到端演示（需先启动上面两个服务）：

```bash
PYTHONPATH=. python scripts/demo_requests.py
```

## 4. HTTP 接口

| 方法 | 路径 | 说明 |
|------|------|------|
| GET  | `/health` | 健康检查 |
| GET  | `/issuers` | 受信发行方清单（不含任何密钥材料） |
| POST | `/verify` | 验证 JWT，body `{"token": "...", "expected_aud"?: "..."}` |
| GET  | `/admin/issuers` | 每个发行方的 JWKS 缓存快照 |
| POST | `/admin/issuers/{id}/refresh` | 强制刷新某发行方缓存（密钥轮换） |

成功：

```json
{ "valid": true, "issuer_id": "demo-rsa", "alg": "RS256",
  "kid": "rsa-1", "claims": { "iss": "...", "aud": "...", "exp": 1790252341 } }
```

拒绝（400 结构问题 / 401 信任·签名·时间问题）：

```json
{ "error": "alg_not_allowed",
  "reason": "算法 'HS256' 不在发行方 'demo-rsa' 的显式允许列表中",
  "context": { "alg": "HS256", "issuer_id": "demo-rsa",
               "allowed_algs": ["PS256","RS256","RS384","RS512"] },
  "denied": true }
```

完整接口、错误码表与攻击复现见 **[`examples/HTTP_API.md`](examples/HTTP_API.md)**。

## 5. 发行方配置

`config/issuers.json` 顶层 `{"issuers": [...]}`，每项：

| 字段 | 必填 | 说明 |
|------|------|------|
| `id` | 是 | 服务端内部唯一 id（缓存按它隔离） |
| `iss` | 是 | 令牌 `iss` 必须精确等于的值 |
| `allowed_algs` | 是 | **显式**算法白名单，仅支持上文列出的算法 |
| `audiences` | 是 | 允许的 `aud` 集合，需与令牌有交集 |
| `jwks_uri` | 非对称必填 | 服务端受信 JWKS 地址（https，或环回 http） |
| `hmac_secret_b64` / `hmac_secret` | 对称必填 | HMAC 密钥（≥32 字节，仅存服务端） |
| `leeway` | 否 | 时钟偏移容忍秒数，默认 0 |
| `cache_ttl` | 否 | 本地 JWKS 缓存秒数，默认 300；远程 `Cache-Control: max-age` 只能缩短不能拉长 |

配置启动时做强校验：未知算法、弱 HMAC 密钥（<32B）、非环回 http、
重复 id/iss、非对称发行方缺 jwks_uri 等都会直接启动失败。

## 6. 验证流水线（顺序即安全边界）

```
3 段紧凑结构 → 严格 base64url（字符集+填充）→ 严格 JSON（拒绝重复键）
→ 头字段类型 / 拒绝 zip / 拒绝 jku,jwk,x5u,x5c,x5t / crit 必须可识别
→ 强制非空 kid
→ iss 索引服务端配置（未知即拒，绝不推断取钥地址）
→ alg ∈ 该发行方白名单（none/未列出算法在此拒绝，先于空签名判断）
→ 取钥：非对称走该发行方隔离缓存；对称只用服务端密钥
→ JWK kty/曲线/alg 约束必须与 alg 匹配 → cryptography 实际验签
→ exp（必填，now==exp 即过期）→ nbf（now==nbf 恰好生效）→ iat
→ aud 与该发行方集合有交集（可叠加请求级 expected_aud）
```

## 7. 缓存与轮换语义

- 每个发行方独立 `IssuerJwksCache`（独立 dict、锁、TTL），按 `id` 注册。
- 新鲜缓存内不认识的 `kid` 直接 `kid_not_found`，**不会**因单次 miss 反复回源。
- TTL 过期后回源；回源成功才整体替换钥匙集，不会出现半份新密钥污染。
- 回源失败：有旧缓存且命中则**降级用旧缓存**；无缓存则进入 10s 失败冷却。
- 远程 JWKS 文档内出现重复 `kid` → 整份拒绝（`duplicate_kid_in_jwks`），
  继续保留旧缓存；含私钥字段（`d/p/q/...`）的 JWK 也拒绝。
- 轮换可等待自然过期，或调 `POST /admin/issuers/{id}/refresh` 立即生效。

## 8. 验收点与对应测试

| 验收要求 | 测试文件 |
|----------|----------|
| 算法混淆（RS→HS、none、跨族、JWK alg 约束） | `tests/test_algorithm_confusion.py` |
| 重复 kid（文档内拒绝、投毒时保旧缓存） | `tests/test_headers_and_kid.py` |
| 轮换缓存（TTL、强制刷新、滚动双钥匙） | `tests/test_cache.py` |
| 缓存按发行方隔离、故障降级、冷却 | `tests/test_cache.py` |
| exp/nbf 边界时刻 + leeway 等值边界 | `tests/test_time_and_claims.py` |
| aud / 声明类型 / bool 时间戳 | `tests/test_time_and_claims.py` |
| 禁止令牌指定取钥地址（jku/jwk/x5u…） | `tests/test_headers_and_kid.py`、`test_e2e_http.py` |
| 结构化拒绝原因、日志不含完整令牌 | `tests/test_malformed_and_logging.py` |
| 真实 HTTP JWKS 端到端 | `tests/test_e2e_http.py` |
| 配置（信任根）校验 | `tests/test_config.py` |

实际运行结果与已知限制见 **[`RUN_LOG.md`](RUN_LOG.md)**。

## 9. 依赖

运行时仅三个直接依赖（锁定版本见 `requirements-lock.txt`）：

- `fastapi==0.141.1`
- `uvicorn[standard]==0.53.0`
- `cryptography==50.0.1`

测试另用 `pytest`、`httpx`（均已在 lock 文件中）。
