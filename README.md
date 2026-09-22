# 本地域名生命周期引擎 (domainengine)

一个完全本地化的域名注册生命周期引擎：Go + Echo + sqlx + PostgreSQL + Docker Compose。
不调用任何真实注册商、DNS 或支付服务；转售商使用本地积分账本结算。

## 快速开始

```bash
docker compose up -d --build        # 启动 PostgreSQL + 应用（自动执行迁移和种子数据）
curl localhost:18086/api/prices -H 'X-Api-Key: admin-key'
```

- API 监听宿主机 `18086` 端口（容器内 8080），数据库暴露宿主机 `25432` 端口。
- 迁移文件内嵌于 `internal/migrate/migrations/`，应用启动时自动按序执行（`schema_migrations` 表记录，重复执行安全）。
- 种子数据：管理员、两个转售商、三个客户、价格表和初始积分（各 5000.00）。

### 种子账号（`X-Api-Key` 头认证）

| 角色     | 名称       | API Key          |
|----------|------------|------------------|
| admin    | Admin      | `admin-key`      |
| reseller | Reseller A | `reseller-a-key` |
| reseller | Reseller B | `reseller-b-key` |
| customer | Alice (A)  | `alice-key`      |
| customer | Bob (A)    | `bob-key`        |
| customer | Carol (B)  | `carol-key`      |

### 本地运行（不用 Docker 跑应用）

```bash
docker compose up -d db
DATABASE_URL="postgres://postgres:postgres@localhost:25432/domains?sslmode=disable" \
AUTH_CODE_KEY="000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f" \
ENABLE_CLOCK_CONTROL=true \
go run ./cmd/server
```

`AUTH_CODE_KEY` 为 64 位十六进制（32 字节），用于加密转移授权码；不设置时会生成临时密钥（重启后已签发的授权码无法解密，仅适合开发）。

## 生命周期状态机

```
                 Register(收费)
   available ──────────────────► registered ──► transferring ──► registered(转移完成: 扣款+续1年)
      ▲                            │  ▲             │  ▲
      │                            │  │Renew(收费)   │  │批准 → 模拟等待5天 → 完成
      │                            ▼  │             │  │取消/拒绝/超时 → 释放冻结积分
      │                          expired────────────┘  │
      │                            │(过期宽限期, 可续费) │
      │                            ▼                    │
      │                         redemption(30天赎回期, 赎回价续费)
      │                            │
      │                            ▼
      │                        pending_delete
      │                            │
      └────────── 删除行 ◄─────────┘
```

合法转换与计费时点（金额一律为整数美分）：

| 转换 | 触发 | 计费 |
|---|---|---|
| available → registered | `POST /api/domains` | 立即扣注册价 × 年数 |
| registered/expired → registered | `POST /api/domains/:name/renew` | 立即扣续费价 × 年数 |
| redemption → registered | 同上（赎回） | 立即扣赎回价 × 年数 |
| registered → transferring | `POST /api/transfers`（16 位授权码） | **冻结**转移价（credit hold） |
| transferring → registered | 批准后模拟等待 5 天，后台任务完成 | **捕获**冻结（真正扣款），到期时间 +1 年 |
| transferring → registered | 取消 / 拒绝 / 审批超时（默认 7 天） | **释放**冻结，不扣款 |
| registered → expired → redemption → pending_delete → 删除 | 后台任务按 `expires_at` 与宽限期推进 | 不收费 |

时间参数均可用环境变量覆盖（天数）：`EXPIRED_GRACE_DAYS`(30)、`REDEMPTION_DAYS`(30)、
`PENDING_DELETE_DAYS`(5)、`TRANSFER_WAIT_DAYS`(5)、`TRANSFER_APPROVAL_TIMEOUT_DAYS`(7)。

## 域名规范化规则

同一规范化域名只能注册一次（`domains.name` 唯一约束）。规范化（`internal/domainname`）：

1. 去除首尾空白；
2. 去除末尾点：`example.com.` ≡ `example.com`（DNS 绝对形式等价）；
3. ASCII 大小写折叠：`ExAmPle.COM` ≡ `example.com`（DNS 大小写不敏感）；
4. 国际化域名按 IDNA 转 punycode A-label 存储：`münchen.de` ≡ `xn--mnchen-3ya.de`，
   Unicode 写法与 punycode 写法会冲突在同一个唯一键上；
5. 校验每个 label：1–63 字符、字母数字加连字符、不得以连字符开头/结尾，全长 ≤ 253。

## 并发与一致性设计

- **抢注**：并发注册依赖 `UNIQUE(name)`，只有赢家提交，输家得 409，且只有赢家扣款
  （扣款与状态变更在同一事务）。
- **幂等**：所有变更类接口要求 `idempotency_key`（按用户隔离）。键与响应在同一事务写入；
  重试直接回放原响应，不重复执行、不重复扣费；失败的请求整体回滚、不记录键，
  充值后可用原键重试。并发同键请求在唯一索引上串行化，只有一个执行。
- **续费 vs 到期清理竞争**：续费对域名行 `SELECT ... FOR UPDATE`，到期任务用条件 UPDATE
  抢同一行锁。无论谁先提交，最终结果确定：域名回到 `registered`、只扣一次费
  （过期后续费仍按续费价，赎回期内按赎回价）。
- **余额并发**：扣款/冻结前先 `SELECT ... FOR UPDATE` 锁定转售商行，再校验可用余额
  （余额 − 未决冻结），并发扣款不会双双通过。
- **后台任务可重启续跑**：到期推进全部是条件 UPDATE；转移完成使用确定性幂等键
  （`transfer:<id>:capture`）捕获冻结，进程在任意两步之间崩溃后重跑不会重复扣款；
  重复执行维护任务是空操作（有恢复测试覆盖）。
- **价格变更不改写已受理交易**：价格表只追加（`effective_from` 生效），注册/转移在受理时
  把价格固化进账本条目和 `transfers.price_cents`，之后调价不影响在途交易。

## 权限模型

- **客户**：只能看/操作自己的域名；注册、续费、生成授权码、发起/取消转移、批准转入自己域名的转移。
- **转售商**：只能查看所属客户的域名与相关转移；查看自己的余额与账本。
- **管理员**：全量查看；管理价格（`PUT /api/admin/prices`）；给转售商充值
  （`POST /api/admin/resellers/:id/credits`）；时钟控制（如启用）。
- **授权码**：16 位字符（去除易混淆字符），AES-256-GCM 加密存储（`auth_code_enc`），
  仅所有者可生成/查看；日志只记录请求元数据（方法/路径/状态码），不记录请求体，
  授权码不会进入日志；转移校验使用恒定时间比较。

## 模拟时间示例

时钟可注入：所有生命周期计算都基于 `clock.Clock` 接口（测试用假时钟，无需真实等待）。
本地演示时设置 `ENABLE_CLOCK_CONTROL=true`，管理员可推进时间（单调递增）：

```bash
# 注册一个域名
curl -X POST localhost:18086/api/domains -H 'X-Api-Key: alice-key' \
  -d '{"name":"demo.com","years":1,"idempotency_key":"k1"}'

# 快进 31 天：进入 expired；再快进 30 天：redemption；再快进 5 天：pending_delete → 删除
curl -X POST localhost:18086/api/admin/clock/advance -H 'X-Api-Key: admin-key' -d '{"seconds":2678400}'
curl localhost:18086/api/admin/clock -H 'X-Api-Key: admin-key'
curl localhost:18086/api/domains/demo.com -H 'X-Api-Key: alice-key'

# 转移流程：授权码 → 发起 → 批准 → 快进 5 天 → 自动完成
curl -X POST localhost:18086/api/domains/demo.com/auth-code -H 'X-Api-Key: alice-key'
curl -X POST localhost:18086/api/transfers -H 'X-Api-Key: carol-key' \
  -d '{"domain":"demo.com","auth_code":"<16位授权码>","idempotency_key":"t1"}'
curl -X POST localhost:18086/api/transfers/<transfer-id>/approve -H 'X-Api-Key: alice-key'
curl -X POST localhost:18086/api/admin/clock/advance -H 'X-Api-Key: admin-key' -d '{"seconds":432000}'
```

## API 一览

| 方法/路径 | 角色 | 说明 |
|---|---|---|
| `POST /api/domains` | customer | 注册 `{name, years, idempotency_key}` |
| `GET /api/domains` | 所有 | 按角色范围列出域名 |
| `GET /api/domains/:name` | 所有者/所属转售商/admin | 域名详情 |
| `POST /api/domains/:name/renew` | 所有者/admin | 续费/赎回 `{years, idempotency_key}` |
| `POST /api/domains/:name/auth-code` | 所有者/admin | 生成 16 位授权码（重置旧的） |
| `GET /api/domains/:name/auth-code` | 所有者/admin | 查看当前授权码 |
| `POST /api/transfers` | customer | 发起转移 `{domain, auth_code, idempotency_key}` |
| `POST /api/transfers/:id/approve` | 现所有者/admin | 批准（进入 5 天等待） |
| `POST /api/transfers/:id/reject` | 现所有者/admin | 拒绝（释放冻结） |
| `POST /api/transfers/:id/cancel` | 发起方/admin | 取消（释放冻结） |
| `GET /api/transfers` | 所有 | 按角色范围列出转移 |
| `GET /api/resellers/me/balance` | reseller | 余额 / 冻结 / 可用 |
| `GET /api/resellers/me/ledger` | reseller | 追加式账本 |
| `GET /api/prices` | 所有 | 当前价格 |
| `PUT /api/admin/prices` | admin | 追加价格 `{tld, action, amount_cents, effective_from?}` |
| `POST /api/admin/resellers/:id/credits` | admin | 充值 `{amount_cents, idempotency_key}` |
| `POST /api/admin/clock/advance` | admin | 快进模拟时钟 `{seconds}`（需启用） |

## 测试

```bash
docker compose up -d db
TEST_DATABASE_URL="postgres://postgres:postgres@localhost:25432/domains?sslmode=disable" go test ./...
# 竞争与竞态检测
TEST_DATABASE_URL=... go test -race -count=2 ./...
```

覆盖：并发抢注（唯一归属+单次扣费）、幂等重试、失败不扣款不记录、续费边界
（到期瞬间/宽限期/赎回期/待删除）、续费与清理竞争的确定结果、转移全生命周期
（5 天等待、完成扣款）、转移审批超时释放冻结、取消释放冻结、错误授权码与越权、
价格变更不改写已受理交易、维护任务崩溃重跑的幂等恢复。未设置
`TEST_DATABASE_URL` 时集成测试自动跳过，纯单元测试（规范化等）照常运行。

## 目录结构

```
cmd/server/            入口：配置、迁移、HTTP 服务、后台任务、优雅退出
internal/clock/        可注入时钟（真实/偏移/假时钟）
internal/domainname/   域名规范化（大小写、末尾点、IDN/punycode）
internal/service/      业务核心：状态机、账本、转移、维护任务、定价
internal/httpapi/      Echo 路由、认证中间件、错误映射
internal/jobs/         周期维护任务（可重启续跑）
internal/migrate/      内嵌 SQL 迁移
```
