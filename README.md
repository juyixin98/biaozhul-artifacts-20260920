# 社区内容治理后端 (Community Content Governance API)

Go + Chi + sqlc + PostgreSQL 实现的社区内容治理服务：会员层级授权、收款登记
与续费、投稿审核状态机（不可变版本）、举报受理/裁决/一次申诉、课程发布冻结。
不含支付网关、聊天和前端。

## 快速开始（Docker）

```bash
docker compose up -d --build          # 启动 PostgreSQL + 迁移 + API(:8080)
docker compose --profile seed run --rm seed   # 可选：写入示例数据
curl localhost:8080/healthz           # 204
```

数据库映射到宿主机 `55432`（避免与本机 5432 冲突）。示例数据执行后会打印
各角色的 Bearer Token。

## 本地开发

```bash
# 需要 Go 1.22、sqlc 1.25、PostgreSQL 16
createdb gov && psql -d gov -c "CREATE USER gov PASSWORD 'gov';"  # 或自行配置
export DATABASE_URL="postgres://gov:gov@localhost:5432/gov?sslmode=disable"

make sqlc        # 从 internal/dbquery/*.sql 重新生成 internal/database
make migrate     # 应用迁移（嵌入二进制，也可由服务启动时自动执行）
make seed        # 写入示例数据
make run         # 启动 HTTP 服务
make test        # 真实 PostgreSQL（testcontainers）集成测试，含并发场景
```

环境变量：`DATABASE_URL`、`HTTP_ADDR`（默认 `:8080`）。

## 认证与角色

除 `/healthz` 外所有接口需要 `Authorization: Bearer <token>`。

**首次引导**：空系统没有任何用户。服务读取环境变量
`PLATFORM_BOOTSTRAP_TOKEN`（compose 默认为 `tok_platform_bootstrap`），
用该 token 调 `POST /admin/communities` 即可创建第一个社区并得到管理员
token。该引导 token 是“平台管理员”，**不能**访问任何社区内接口
（所有社区作用域路由对它返回 404）。完成引导后可在部署中清空该环境变量。

| 角色 | 能力 |
|---|---|
| `admin` | 建用户、建/停用层级、登记收款并续费、取消会员、审核 |
| `moderator` | 审稿（批准/驳回/下架/恢复）、处理举报；**不能登记收款续费** |
| `member` | 投稿、编辑自己的内容、举报、申诉；按有效层级读取已发布版本 |

作者**不能审核自己的内容**（接口强制 403）。

## 核心规则

### 会员与收款

- 每社区最多 **10** 个层级（数据库触发器强制），层级 1–10，金额为**整数分**。
- 管理员登记**线下收款**后立即延长有效期；续费原子执行
  `period_end = greatest(period_end, now()) + N天`，不丢失剩余时长；
  并发首单使用 `INSERT ... ON CONFLICT DO UPDATE`，不会产生唯一键冲突。
- 收款以 **request_id 幂等**：同 ID 同内容视为重放；**同 ID 不同内容 → 409**。
- 访问条件：`status='active' AND period_end > now()`（边界是严格大于）。
  到期即失效；取消立即失效（即使未到期）。

### 投稿状态机与不可变版本

```
draft ──submit──▶ pending ──approve──▶ published
 ▲                   │                    │
 └──── reject ───────┘              delist▼
                                   delisted
```

- 每次修改都**新增**不可变版本（`content_versions` 永不更新），附件冻结在
  具体版本上。
- 提交与批准都**绑定版本号**：批准 SQL 同时要求
  `status='pending' AND current_version_id = :version_id`。作者在审核期间
  编辑会生成新版本并把内容退回 draft，此时并发批准匹配 0 行返回 409，
  **未经审核的新版本永远不会被发布**。
- 恢复操作要求恢复的版本仍是最新版本；下架后作者若已提交更新版本，
  恢复旧版本返回 409，旧版不能覆盖新修改。

### 举报

- 举报绑定**目标版本**、类别、理由。
- 流程：创建（accepted/受理）→ `uphold`（裁决违规，立即下架）或
  `dismiss`（裁决不违规）→ 作者可**申诉一次** → 申诉裁决
  `appeal_upheld` / `appeal_dismissed`。
- 每个动作写入操作者与依据（`report_events` + `content_events`）。

### 课程

- 课程 = 有序模块 → 有序课时，课时引用某个内容的具体版本。
- 发布时把结构与版本**冻结**到快照表；之后编辑草稿不影响已发布课程。
- 重新发布会**重新校验全部课时**：引用版本必须是内容当前已发布版本、
  内容未下架、内容层级不高于课程层级。
- 课时读取与直接读取内容走**同一个鉴权函数**：内容下架/降级后课程内
  立即同样失去访问。

### 一致性隔离

正文（`GET /versions/{id}`）、附件下载（`GET /attachments/{id}`）、
导出（`GET /contents/{id}/export`，zip）全部经过同一鉴权检查：
内容已发布、请求版本即冻结的发布版本、会员有效且层级达标。

## HTTP 接口

完整字段见 `docs/API.md`。主要端点：

```
POST   /admin/communities                      任意已认证用户创建社区并成为管理员
POST   /communities/{cid}/users                admin 创建用户（返回 token）
POST   /communities/{cid}/tiers                admin 创建层级（最多10）
POST   /communities/{cid}/tiers/{id}/active    admin 启用/停用层级
POST   /communities/{cid}/payments             admin 登记收款并续费（幂等）
GET    /communities/{cid}/me/subscription
POST   /communities/{cid}/me/cancel[?user_id]

POST   /communities/{cid}/contents             创建草稿+首个版本
POST   /contents/{id}/versions                 追加不可变版本（作者）
POST   /contents/{id}/submit                   提交审核（body: version_id）
POST   /contents/{id}/approve                  审核员通过（version_id+预期状态）
POST   /contents/{id}/reject                   审核员驳回（version_id, reason）
POST   /contents/{id}/delist                   下架
POST   /contents/{id}/restore                  恢复（绑定版本，旧版不能覆盖新版）
GET    /versions/{id}                          读取版本正文（统一鉴权）
POST   /versions/{id}/attachments              作者给当前版本加附件（base64）
GET    /attachments/{id}                       下载附件（统一鉴权）
GET    /contents/{id}/export                   导出 zip（统一鉴权）

POST   /contents/{id}/reports                  举报（version_id, category, reason）
POST   /reports/{id}/accept | uphold | dismiss 受理 / 裁决
POST   /reports/{id}/appeal                    作者一次申诉
POST   /reports/{id}/appeal/uphold | /dismiss  申诉裁决
GET    /reports/{id}/events                    裁决/操作审计

POST   /communities/{cid}/courses              建课程（含草稿结构）
PUT    /courses/{id}/structure                 改草稿（不影响已发布快照）
POST   /courses/{id}/publish                   冻结并发布（全量重新校验）
GET    /courses/{id}                           已发布返回冻结快照
GET    /courses/{id}/lessons/{position}        读课时（走内容统一鉴权）
```

## 测试

`internal/integration` 使用 testcontainers 启动真实 PostgreSQL 16，覆盖：

- `TestConcurrentRenewalNeverLosesDays`：20 并发续费，时长精确累加
- `TestIdempotentPaymentSameRequestID`：同 request_id 重放/冲突
- `TestExpirationAndCancelCutAccessImmediately`：到期边界（严格 `>`）与取消即时失效
- `TestReviewRaceEditAndApprove`：编辑与批准并发，未审核版本绝不发布
- `TestRestoreCannotClobberNewerEdit`：恢复旧版不能覆盖之后的新修改
- `TestVersionsImmutable`：版本不可变
- `TestBodyAttachmentExportConsistentIsolation`：正文/附件/导出一致隔离
- `TestCoursePublishFreezes` / `TestCoursePublishRejects...`：发布冻结与重新校验
- `TestReportAppealOnceAndAudit`：受理→裁决→一次申诉→恢复审计
- `TestAuthorCannotSelfApprove` / `TestModeratorCannotRenew`：角色越权

```bash
make test          # 如 Docker API 较老可： DOCKER_API_VERSION=1.44 make test
```

## 目录结构

```
cmd/server       HTTP 服务入口
cmd/migrate      独立迁移命令
cmd/seed         示例数据
internal/migrate 嵌入迁移（internal/migrate/sql/*.sql）
internal/dbquery sqlc 查询源（并发关键操作均为单条行锁 SQL）
internal/database sqlc 生成代码（pgx/v5）
internal/app      Chi 路由与处理器；统一鉴权 choke-point 在 helpers.go
internal/integration 真实数据库集成测试
```
