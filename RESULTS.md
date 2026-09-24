# 实际运行结果

记录时间：2026-09-24（UTC）。环境：Ubuntu、Python 3.12.3、全新空目录初始化。

## 1. 环境搭建

- `python3 -m venv .venv` 成功。
- 依赖从 PyPI 安装成功。说明：本机镜像未提供旧版本，最初的区间约束
  （`pytest-asyncio>=0.24,<1.0`）无法解析，已放宽 dev 约束为
  `pytest-asyncio>=0.24`，并用实际安装版本冻结到 `requirements.lock`。
  运行时依赖（`requirements.txt`）均在声明区间内解析成功。
- 冻结的关键版本（完整清单见 `requirements.lock`）：

  | 包 | 版本 |
  |---|---|
  | fastapi | 0.141.1 |
  | starlette | 1.7.0 |
  | uvicorn | 0.53.0 |
  | pydantic | 2.13.5 |
  | cryptography | 45.0.7 |
  | httpx | 0.28.1 |
  | pytest | 9.1.1 |
  | pytest-asyncio | 1.4.0 |

  注：`requirements.lock` 为 `pip freeze` 全量冻结，含测试依赖；
  生产仅装运行时依赖时使用 `pip install -r requirements.txt`。

## 2. 自动化测试

命令：

```bash
.venv/bin/pytest -q
```

结果：

```
67 passed in 4.02s
```

测试分布：

- `tests/test_verifier.py`：46 个 —— 验证主管线
  - 正常路径（RS256/ES256、显式 issuer_id）
  - 算法混淆：`none`、RS256→HS256（用 RSA 公钥 DER 当 HMAC 密钥）、
    ES256 令牌发给仅允许 RS256 的发行方、跨发行方重放
  - 头部注入：`jku` / `x5u` / 内嵌 `jwk` / `crit`、缺 kid、空签名
  - 边界时刻：`exp == now` 放行、过期 1 秒拒绝；`nbf == now` 放行、
    未来 1 秒拒绝；leeway 宽限；未来 `iat`；非数字时间声明
  - 声明：aud 不匹配、aud 数组、iss 签名后复核、未知/缺失发行方
  - 签名：篡改 payload、他键同 kid 伪造、结构损坏、空令牌
  - 轮换与缓存：未知 kid 恰好触发一次刷新、负缓存冷却取数计数、
    TTL ±1 秒边界、管理端强制刷新后旧键失效、刷新失败保留旧好键、
    两发行方同 kid 隔离、跨发行方流量不互相取钥
  - 重复 kid：整份 JWKS 拒绝（`duplicate_kid`）；
    被禁算法造成的“假重复”不误报
  - JWKS 异常：冷缓存取钥失败、非 JSON 文档、JWK alg 与键类型不符
- `tests/test_jwks.py`：15 个 —— JWKS 解析与 HTTP 取钥器
  （重复 kid、对称键跳过、非签名用途、弱 1024 位 RSA 拒绝、
  alg/键类型不符、非有限数；重定向/非 200/超长/错误 content-type/网络错误）
- `tests/test_api.py`：9 个 —— HTTP 接口、结构化错误信封、
  Bearer 头、管理端点
- `tests/test_logging.py`：2 个 —— 审计字段不含令牌；
  用金丝钥串构造 HMAC 混淆令牌，断言日志中既无完整令牌也无金丝串，仅有指纹

开发过程中出现过 3 轮失败，均已修复并复验通过：缺导入（NameError）、
空签名段在结构解析阶段被提前拒绝（调整为头部策略之后再判）、
未知 kid 成功刷新后未设置负缓存（已修，保证取钥风暴防护）、
刷新失败应回退旧好键而非直接拒绝（已修）。

## 3. 端到端演示

命令：

```bash
bash scripts/run_demo.sh
```

本地模拟 IdP（标准库 HTTP 服务 + cryptography 签发）+ 网关真实 uvicorn
进程联调，12 个场景实测结果：

| # | 场景 | 实测 |
|---|---|---|
| 1 | 合法 RS256 令牌 | **200 ok**，返回 claims |
| 2 | 合法 ES256 令牌（第二发行方） | **200 ok** |
| 3 | `alg: none` | **401 algorithm_not_allowed** |
| 4 | RS256→HS256 算法混淆（公钥当 HMAC 密钥） | **401 algorithm_not_allowed**（取钥前拒绝，details 带 alg=HS256 与允许列表） |
| 5 | `jku` 指向攻击者主机 | **401 header_key_reference_forbidden** |
| 6 | 过期令牌 | **401 token_expired**（details 带 exp/now/leeway） |
| 7 | nbf 在未来 | **401 token_not_yet_valid** |
| 8 | aud 不匹配 | **401 invalid_audience** |
| 9 | 攻击者自造键、复用真 kid | **401 invalid_signature** |
| 10 | kid 不存在 | **401 unknown_kid**（触发恰好一次 JWKS 刷新） |
| 11 | JWKS 中同 kid 两把不同 RSA 键 | **401 duplicate_kid** |
| 12 | 轮换：旧令牌 → rotate → 管理端 refresh → 新令牌 | 旧 **401 unknown_kid**；新 **200 ok**（kid rsa-k2） |

网关审计日志实测样例（完整令牌从未出现，仅有 16 位 SHA-256 指纹）：

```json
{"level": "WARNING", "message": "token rejected", "event": "token_verification",
 "outcome": "rejected", "token_fingerprint": "sha256:793901dd717d447e",
 "issuer_id": "demo-dup", "error_code": "duplicate_kid"}
{"level": "INFO", "message": "token accepted", "event": "token_verification",
 "outcome": "accepted", "token_fingerprint": "sha256:b831dcf2b4b6d2a8",
 "issuer_id": "demo-rsa", "kid": "rsa-k2", "alg": "RS256"}
```

## 4. 未完成项 / 已知限制

1. `/admin/*` 端点未加鉴权，生产需置于内网或加网络层鉴权。
2. JWKS 抓取使用系统信任库，未做证书钉扎；未做私网/元数据地址的 SSRF
   出口过滤（jwks_uri 仅来自本地配置且强制 https，风险已受限）。
3. 缓存为单进程内存缓存，多实例间不共享（不影响正确性，只影响取钥次数）。
4. 仅支持 RS256/384/512、ES256/384；未支持 RS-PSS、EdDSA、`x5c` 证书链。
5. 无速率限制/请求体大小之外的通用 WAF 能力；聚焦验证逻辑本身。
6. 时间声明按整数 NumericDate 校验（RFC 常规用法），不接受小数秒。
