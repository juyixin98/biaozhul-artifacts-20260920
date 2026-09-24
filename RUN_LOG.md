# 运行记录（如实）

记录时间：2026-09-24
环境：Linux 6.8 / Python 3.12.3 / venv；依赖按 `requirements-lock.txt` 安装。

## 1. 自动化测试

命令：

```bash
python3 -m venv .venv && source .venv/bin/activate
python -m pip install -r requirements-lock.txt
python -m pytest -q
```

结果（最后一次完整运行）：

```
80 passed, 1 warning in 6.25s
```

唯一 warning 来自 starlette TestClient 对 `httpx` 的弃用提示
（`Using httpx with starlette.testclient is deprecated; install httpx2 instead`），
不影响功能；未为消除该第三方提示而引入测试专用的 `httpx2` 包。

静态检查：`python -m pyflakes app scripts tests` → 无告警。

测试分布（共 80 个）：

| 文件 | 关注点 |
|------|--------|
| `test_happy_path.py` | RS/PS/ES/EdDSA/HS 各算法成功路径、aud 列表、请求级 expected_aud、篡改 |
| `test_algorithm_confusion.py` | RS256→HS256 混淆（用公钥 PEM 当 HMAC 密钥）、`none` 各大小写、跨族、JWK alg 约束、混合配置下 HS256 不用公钥、未知算法 |
| `test_headers_and_kid.py` | jku/jwk/x5u/x5c/x5t 禁参、crit、缺 kid、未知 iss、kid 不存在、JWKS 重复 kid、重复 kid 投毒保旧缓存、重复 JSON 键、JWKS 含私钥字段 |
| `test_cache.py` | 发行方隔离（同 kid 不同钥匙）、同 kid 轮换、双 kid 滚动、空缓存故障拒绝、过期缓存故障降级、可观测性、max-age 不拉长 TTL |
| `test_time_and_claims.py` | `now==exp`/`now==nbf` 等边界与 leeway 等值边界（参数化 10 例）、exp 必填、aud、非数字/布尔时间戳、未来 iat |
| `test_malformed_and_logging.py` | 畸形结构、zip、错误体固定字段、日志不含完整令牌及任一段、成功日志只含元数据 |
| `test_e2e_http.py` | 真实 `http.server` + 生产 `UrllibJwksFetcher`：回源一次后命中缓存、jku 不触发任何外联、管理接口轮换 |
| `test_config.py` | 信任根配置的 10 项启动期校验 |

## 2. 真实服务手工/脚本验证

启动（注意：本机 8080 已被另一个系统服务占用，网关改用 **8090**）：

```bash
# 终端 1：demo 发行方
PYTHONPATH=. python scripts/demo_issuer.py            # 127.0.0.1:8787

# 终端 2：网关
JWT_GATEWAY_CONFIG=config/issuers.json PYTHONPATH=. \
  python -m uvicorn app.main:app --host 127.0.0.1 --port 8090

# 自动跑 11 个场景
GATEWAY_URL=http://127.0.0.1:8090 PYTHONPATH=. python scripts/demo_requests.py
```

`demo_requests.py` 退出码 0。各场景实测结果：

| # | 场景 | 实测 |
|---|------|------|
| 1 | RS256 正常令牌 | 200 `valid:true, issuer:demo-rsa` |
| 2 | EdDSA 正常令牌 | 200 `issuer:demo-ec, kid:ed-1` |
| 3 | HS256 正常令牌（无 JWKS 外联） | 200 `issuer:demo-hmac` |
| 4 | 过期 60s | 401 `token_expired`（context 含 exp/now/overdue_s） |
| 5 | nbf 在未来 120s | 401 `token_not_yet_valid`（含 wait_s） |
| 6 | aud 不匹配 | 401 `aud_not_allowed` |
| 7 | RS256→HS256 混淆（公钥 PEM 当密钥） | 401 `alg_not_allowed`，context 回显白名单 |
| 8 | jku 指向 `attacker.example` | 400 `header_parameter_forbidden`，未发生任何外联 |
| 9 | 未知 iss | 401 `unknown_issuer`，未推断/请求任何 JWKS |
| 10 | 发行方轮换密钥 | 旧令牌 200 → 轮换后新令牌 401（缓存未动）→ 管理接口 refresh → 新令牌 200、旧令牌 401 |
| 11 | 缓存快照 | 两发行方各自独立，`cached_kids` 互不干扰 |

日志检查（真实 uvicorn 进程）：对一个合法令牌与 `a.b.c` 畸形令牌分别验证后，

```
grep -F -c "$TOKEN"               gateway.log  -> 0
grep -F -c "$HEADER_SEG"          gateway.log  -> 0
grep -F -c "$PAYLOAD_SEG"         gateway.log  -> 0
grep -F -c "$SIGNATURE_SEG"       gateway.log  -> 0
```

日志行实际形态：

```
... INFO jwt_gateway: verify accepted issuer_id=demo-hmac kid=hmac-1 alg=HS256 fingerprint=sha256:760097349130
... WARNING jwt_gateway: verify denied code=bad_base64url fingerprint=sha256:845e30448809 context={}
```

## 3. 开发过程中发现并修复的问题（如实记录）

1. **`alg=none` 曾被报成 `invalid_signature`**：初版在算法白名单之前
   先判空签名。已把空签名判断移到发行方与算法白名单之后，`none`
   现在稳定返回 `alg_not_allowed`（有回归测试）。
2. **`@@@` 被报成 `bad_json` 而非 `bad_base64url`**：Python
   `urlsafe_b64decode` 会静默丢弃字母表外字符，使非法段变成空 JSON。
   已在 `b64url_decode` 中增加严格字符集校验。
3. **成功验证的 INFO 日志在真实 uvicorn 下不输出**：uvicorn 默认
   dictConfig 只配置 `uvicorn.*` logger，`jwt_gateway` 落到 root
   lastResort（WARNING 级），INFO 被吞；pytest 的 caplog 不走该路径，
   所以测试没暴露。已在生产入口 `create_app_from_env()` 中
   `configure_logging()` 显式配置 stderr handler 与级别
   （`JWT_GATEWAY_LOG_LEVEL` 可覆盖），测试入口 `create_app()` 不经此
   配置，caplog 行为不变。已在真实进程复验成功日志出现且不含令牌。
4. 本机 **8080 端口被系统中另一个服务（build-provenance-verify）占用**，
   演示改用 8090；README 的命令以 8080 为例，端口可自行替换。

## 4. 已知限制 / 未完成项

- **无鉴权的管理接口**：`/admin/issuers*` 没有认证/网络限制，默认只应
  绑定环回或由上游网关加鉴权；未实现管理令牌。
- **JWKS 不做 DNS 钉扎 / 证书 Pinning**：`https` 依赖系统 CA 信任；
  配置加载期禁止非环回明文 http，但没有 SSRF 意义上的私网段解析防护
  （如解析后重绑定）。JWKS 地址来自服务端配置而非请求，风险面可控，
  但生产上仍建议出网白名单。
- **不支持 JWE（加密令牌）、嵌套 JWT 与压缩**：`zip`/`cty` 明确拒绝；
  也不解析 `x5c` 链（头禁参）。
- **对称发行方没有 JWKS**：HMAC 密钥只能来自服务端配置文件
  （≥32 字节），不支持从 KMS/环境密钥管理动态加载。
- **无速率限制/请求体大小以外的网关能力**：定位是纯验证组件。
- 时区全部使用 Unix 时间戳数值比较；`exp/nbf` 为整数秒，亚秒令牌按
  float 处理，边界语义见 README 第 6 节。
- `requirements-lock.txt` 在该 Linux/Python 3.12.3 环境下生成；其他
  平台上 `uvloop` 等带轮子的包可能需要各自的锁文件。
