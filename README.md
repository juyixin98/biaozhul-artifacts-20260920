# 可撤销凭证索引（revcred）

纯后端服务：本地签名凭证的**发行**与**撤销索引**。凭证绑定签发者、主体、用途、
有效期与内容摘要；撤销是只追加的事件日志，历史验证按查询时刻重放，密钥轮换
保留生效区间。所有身份均为合成标识，密钥为本地生成的 ed25519 测试密钥。

技术栈：Go 1.22 · Chi v5 · PostgreSQL 16（pgx/v5）。

## 核心语义

- **凭证**：`{issuer, subject, purpose, not_before, not_after, content_digest, kid, signature}`。
  签名是对规范化载荷（字段定序 `\n` 连接、UTC RFC3339Nano 时间）的真实 ed25519 签名；
  `content_digest` 是内容的 sha256（hex），明文内容不落库。
- **有效期**：半开区间 `[not_before, not_after)` —— `at == not_before` 有效，
  `at == not_after` 已过期。
- **撤销只追加**：`revocation_events` 只有 INSERT。`seq` 是全局单调**快照号**，
  撤销响应与验证响应返回同一序列的快照号。
- **历史验证**：验证请求可带 `at`（RFC3339）。撤销重放只统计
  `recorded_at <= at AND seq <= snapshot` 的事件 —— 过去的结论不会被之后的撤销改写。
- **密钥轮换**：`POST /v1/keys` 生成新密钥并把该签发者旧密钥的 `valid_to` 封闭到
  新密钥的 `valid_from`。旧密钥及其区间保留，轮换前签发的凭证仍用旧公钥验签；
  验证还会检查签发时刻落在所用密钥的生效区间内。
- **缓存**：验证结果缓存键包含快照号。任何撤销都会推进全局快照号，
  撤销前的缓存条目因此永久不可命中，缓存绝不会在撤销后给出旧结论
  （响应中的 `cache_hit` 字段可观测）。

## 目录结构

```
cmd/server/            服务入口（连接数据库、应用迁移、启动 HTTP）
internal/config/       环境变量配置
internal/core/         领域逻辑：发行、撤销、历史验证、快照缓存（Store 为接口）
internal/store/        PostgreSQL 存取（实现 core.Store）
internal/api/          Chi 路由与 JSON 编解码
migrations/            内嵌 SQL 迁移（启动时幂等应用）
examples/              示例输入 JSON 与端到端验收脚本
```

## 本地启动

```bash
make up        # docker compose 启动 PostgreSQL（宿主机端口 55433）并等待就绪
make run       # 启动服务，监听 :8080（自动应用迁移）
```

环境变量：`ADDR`（默认 `:8080`）、`DATABASE_URL`
（默认 `postgres://revcred:revcred@localhost:55433/revcred?sslmode=disable`）。

## 验收命令

```bash
# 1. 自动化测试（真实数据库；含 -race 可选手动加）
make up
make test

# 2. 端到端验收脚本（服务需已在运行）
make run &          # 或另开终端
make acceptance     # 发行→验证→轮换→撤销→历史重放→到期边界，全部断言通过
```

## API 一览

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/healthz` | 健康检查（含数据库连通性） |
| POST | `/v1/keys` | 生成签发者密钥；已有有效密钥时即轮换（封闭旧区间） |
| GET | `/v1/keys?issuer=...` | 列出签发者全部密钥及生效区间 |
| POST | `/v1/credentials` | 发行签名凭证，返回凭证与当前快照号 |
| GET | `/v1/credentials/{id}` | 读取凭证 |
| POST | `/v1/credentials/{id}/revocations` | 追加撤销事件，返回快照号 |
| GET | `/v1/credentials/{id}/revocations` | 列出撤销事件（审计） |
| POST | `/v1/verifications` | 验证：`{credential_id, purpose, at?, content?}` |

### 示例

```bash
# 创建密钥（示例输入见 examples/create-key.json）
curl -s -X POST localhost:8080/v1/keys -d @examples/create-key.json

# 发行凭证（examples/issue.json）
curl -s -X POST localhost:8080/v1/credentials -d @examples/issue.json

# 验证（当前时刻）
curl -s -X POST localhost:8080/v1/verifications -d \
  '{"credential_id":"cred_...","purpose":"age-check"}'

# 历史验证（按过去时刻重放）
curl -s -X POST localhost:8080/v1/verifications -d \
  '{"credential_id":"cred_...","purpose":"age-check","at":"2026-06-01T00:00:00Z"}'

# 撤销（examples/revoke.json），返回的 snapshot 与后续验证响应一致
curl -s -X POST localhost:8080/v1/credentials/cred_.../revocations -d @examples/revoke.json
```

验证响应：

```json
{
  "credential_id": "cred_...",
  "status": "invalid",
  "reasons": ["revoked"],
  "snapshot": 1,
  "checked_at": "2026-09-23T03:46:19.54Z",
  "cache_hit": false
}
```

失败原因码：`signature_invalid`、`key_not_active_at_issuance`、
`purpose_mismatch`、`not_yet_valid`、`expired`、`content_digest_mismatch`、`revoked`。

## 测试覆盖

`internal/core/service_test.go`（真实 PostgreSQL 集成测试）：

- 发行与有效验证、快照号从 0 开始；
- **到期边界**：`not_before` 前后、`not_after` 恰为边界（过期）与前一纳秒（有效）；
- **密钥轮换**：轮换前后签发的凭证各自验签通过，旧密钥区间被封闭且保留，
  交叉验签（用新公钥验旧签名）必须失败；
- **用途不匹配**；
- **撤销与历史重放**：撤销后结论为 revoked 且快照号一致，
  按撤销前时刻的历史验证仍为 valid；
- **缓存失效**：撤销推进快照号后旧缓存条目不再命中；
- **并发撤销**：20 路并发撤销全部成功，事件只追加、快照号两两不同，
  最终快照号等于最大事件序号；
- 内容摘要不匹配、签名被篡改（直接改库）验签失败。

`internal/api/api_test.go`：HTTP 端到端流程（httptest + 真实数据库）。

未设置 `TEST_DATABASE_URL`/`DATABASE_URL` 时集成测试自动跳过；`make test` 会注入连接串。
