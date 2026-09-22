# Community Content Governance

社区内容治理后端：会员层级与付费授权、投稿版本与审核工作流、举报裁决与申诉、课程发布冻结。
纯后端 API（JSON over HTTP），**不含**支付网关、聊天或前端。

技术栈：**Go + Chi + sqlc + PostgreSQL**，本地用 **Docker Compose** 一键启动。

---

## 快速开始

```bash
docker compose up --build
# API: http://localhost:8080
# 服务启动时自动执行迁移（internal/database/migrations_sql）
# 默认不向宿主机暴露 5432（避免与本机 PostgreSQL 冲突）；需要时：
#   docker compose -f docker-compose.yml -f docker-compose.dbport.yml up --build

# 加载示例数据（管理员/审查员/作者/会员 + 层级 + 已发布文章 + 课程）
go run ./cmd/seed
```

不使用 Docker 时：

```bash
# 1. 准备 Postgres 16
export DATABASE_URL='postgres://gov:gov@localhost:5432/gov?sslmode=disable'
# 2. 运行（自动迁移）
go run ./cmd/server
# 3. 示例数据
go run ./cmd/seed
```

环境变量：

| 变量 | 默认值 | 说明 |
|---|---|---|
| `DATABASE_URL` | `postgres://gov:gov@localhost:5432/gov?sslmode=disable` | PG 连接串 |
| `HTTP_ADDR` | `:8080` | 监听地址 |
| `JWT_SECRET` | `dev-insecure-secret-change-me` | HS256 密钥，生产必须覆盖 |
| `JWT_TTL` | `24h` | 令牌有效期（Go duration） |

## 测试（真实 PostgreSQL）

```bash
./scripts/run-tests.sh          # 启动一次性 PG 容器并跑全部测试
RACE=1 ./scripts/run-tests.sh   # 加 -race（并发续期/审核竞争用例）
```

每个测试用例使用独立 schema（`search_path`），可并行、互不干扰。没有数据库时
`go test ./...` 会跳过而不是失败。

代码生成：改 `db/queries/*.sql` 或迁移后运行 `sqlc generate`（sqlc v1.25）。

---

## 角色与职责分离

- **admin（全局）**：创建社区、定义层级、登记收款、取消会员；可参与全部审核。
- **reviewer（社区级）**：被 admin 分配到某社区后可审稿/下架/受理举报；
  **不能登记收款或续费**（支付路由仅 admin；服务层另有防护）。
- **member**：作者投稿；作者**不能审核自己的帖子**。
- 审查员可读取其社区内任意版本用于审稿，但不享有会员内容权限。

认证：`POST /api/register`、`POST /api/login` 返回 JWT；除登录注册外所有接口
要求 `Authorization: Bearer <token>`。每个请求回显/接受 `X-Request-Id`。

---

## 关键不变量（及其实现位置）

1. **最多 10 个层级**：`tiers.level ∈ 1..10`，触发器 `trg_tiers_max_10` +
   服务层计数预检（`membership.CreateTier`）。
2. **整数分**：所有金额 `BIGINT`（`price_cents`/`amount_cents`），无浮点。
3. **请求 ID 幂等 + 冲突**：收款按 `(community_id, request_id)` 唯一；同 ID
   同载荷重放返回原记录且不改有效期，**同 ID 不同载荷 → 409**。
4. **并发续期不丢时长**：`SELECT ... FOR UPDATE` 锁住会员行，续期使用
   `expires_at = GREATEST(expires_at, now()) + n 天`，两笔续费用事务串行化，
   各自时长全部累加（`TestConcurrentRenewalNoLostTime`，8 并发 × 30 天）。
5. **过期/取消立即失权**：唯一内容门禁 SQL 要求
   `m.status='active' AND m.expires_at > now()`，以数据库时钟在语句时刻判定；
   到期边界用严格 `>`（等于 now() 即拒绝），取消即置 `cancelled`。
6. **不可变版本**：修改只追加 `post_versions`；帖子持 `current_version_id` /
   `published_version_id` 双指针。已发布帖子被编辑时，旧批准版继续服务，
   新版经审核通过后原子切换（切换前成员读不到新版）。
7. **审核绑定版本 + 预期状态**：批准 SQL 为
   `UPDATE ... WHERE status=:expected_status AND current_version_id=:version_id`；
   编辑与批准竞争时，若审查员看到的版本不是当前版本，更新 0 行 → 409，
   **未审核的新内容不可能被发布**。审核动作（含失败）全部写入 `reviews`。
8. **举报状态机**：`filed → accepted → upheld/overturned/rejected →（一次）
   appealed → upheld/overturned → restored`。申诉按 `report_decisions` 计数
   **最多一次**；受理、裁决、申诉、恢复都在 `report_decisions` 记录操作者与
   理由（依据）。
9. **恢复不覆盖新修改**：若存在比目标版本更新的版本，恢复操作会以旧内容
   **新建一个不可变版本**（标记 approved），新版本历史原样保留。
10. **课程发布冻结**：发布时把模块/课时顺序与内容版本复制进
    `course_publish_snapshots*`；之后编辑只改草稿表，学习者始终读快照。
    **每次（重新）发布都重新校验全部内容**：引用版本必须仍是对应帖子当前
    已发布版本，否则 409。
11. **正文 / 附件 / 导出一致隔离**：三者共用 `AuthorizeVersion` 与同一条
    `CheckVersionAccess` SQL（层级、有效期、状态、版本四位一体）。

---

## 接口说明

所有路径除 `/api/register`、`/api/login`、`/healthz` 外均需 Bearer JWT。
`:communityID` 为路径参数；请求/响应均为 JSON（附件上传为 multipart，导出为 zip）。

### 认证 / 用户

| 方法 | 路径 | 角色 | 说明 |
|---|---|---|---|
| POST | `/api/register` | – | `{email,name,password,role}`，role=admin/reviewer/member |
| POST | `/api/login` | – | 返回 `{token,user}` |
| GET  | `/api/me` | 已认证 | 当前用户 |
| POST | `/api/communities` | admin | `{name}` |
| GET  | `/api/communities` | 已认证 | 列表 |
| POST | `/api/communities/:cid/reviewers` | admin | `{user_id}` 任命社区审查员 |

### 层级与会员（金额为整数分）

| 方法 | 路径 | 角色 | 说明 |
|---|---|---|---|
| POST | `/api/communities/:cid/tiers` | admin | `{level(1..10),name,price_cents,duration_days}` |
| GET  | `/api/communities/:cid/tiers` | 已认证 | 层级列表 |
| POST | `/api/communities/:cid/payments` | **仅 admin** | 登记线下收款并续期，需 `X-Request-Id` |
| GET  | `/api/communities/:cid/members/:uid` | 已认证 | 会员状态 |
| POST | `/api/communities/:cid/members/:uid/cancel` | admin | 立即取消，即时失权 |

收款请求体：
```json
{ "user_id": 4, "tier_id": 2, "amount_cents": 2900, "extend_days": 90 }
```
头：`X-Request-Id: <uuid>`（幂等键，也可用 body 的 `request_id`）。
同 ID 重放同载荷 → 201 返回原记录；同 ID 不同载荷 → 409。

### 投稿、版本与审核

| 方法 | 路径 | 角色 | 说明 |
|---|---|---|---|
| POST | `/api/communities/:cid/posts` | member+ | 建稿并生成 v1：`{title,body,required_tier_level}` |
| GET  | `/api/communities/:cid/posts` | 已认证 | 列表，支持 `status`、`mine=true` |
| GET  | `/api/communities/:cid/posts/:pid` | 已认证 | 帖子元数据 |
| GET  | `/api/communities/:cid/posts/:pid/versions` | 作者/审核 | 版本历史 |
| POST | `/api/communities/:cid/posts/:pid/versions` | 作者 | 追加不可变版本（待审中禁止） |
| POST | `/api/communities/:cid/posts/:pid/submit` | 作者 | 草稿/已发布改稿 → 待审 |
| POST | `/api/communities/:cid/posts/:pid/withdraw` | 作者 | 首次投稿撤回（pending→draft） |
| POST | `/api/communities/:cid/posts/:pid/review` | 社区审核/admin | `{version_id,approve,reason}` |
| POST | `/api/communities/:cid/posts/:pid/takedown` | 审核/admin | `{reason,include_pending}` 立即下架 |
| POST | `/api/communities/:cid/posts/:pid/restore` | 审核/admin | `{version_id,reason}` |

审核响应说明：`approve=true` 必须携带审查员看到的 `version_id`；服务端还要求
帖子处于可审状态且该版本正是 `current_version_id`，否则 `409`（未审核新内容
不会被发布）。作者审核自己的帖子返回 `403`。

### 内容读取（统一门禁）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/communities/:cid/versions/:vid` | 正文（通过统一门禁） |
| GET | `/api/communities/:cid/versions/:vid/export` | 正文+附件打包 zip |
| POST | `/api/communities/:cid/versions/:vid/attachments` | 作者上传（multipart 字段 `file`） |
| GET | `/api/communities/:cid/attachments/:aid` | 附件下载（同一门禁） |

门禁：已批准 + 帖子已发布且 `published_version_id=该版本` + 会员有效未过期 +
层级足够；审查员/admin 可读任意版本（审稿用），作者可读自己的版本。
跨社区访问返回 404。

### 举报与裁决

| 方法 | 路径 | 角色 | 说明 |
|---|---|---|---|
| POST | `/api/communities/:cid/reports` | member+ | `{post_id,target_version_id,category,reason}` |
| GET  | `/api/communities/:cid/reports` | 审核/admin | 可按 `status` 过滤 |
| GET  | `/api/communities/:cid/reports/:rid` | 相关方/审核 | 详情 |
| GET  | `/api/communities/:cid/reports/:rid/decisions` | 相关方/审核 | 操作与依据流水 |
| POST | `/api/communities/:cid/reports/:rid/accept` | 审核/admin | filed→accepted |
| POST | `/api/communities/:cid/reports/:rid/reject` | 审核/admin | →rejected |
| POST | `/api/communities/:cid/reports/:rid/uphold` | 审核/admin | 成立并**立即下架** |
| POST | `/api/communities/:cid/reports/:rid/overturn` | 审核/admin | 申诉后翻案（内容仍下架） |
| POST | `/api/communities/:cid/reports/:rid/appeal` | 作者或举报人 | **仅一次** |
| POST | `/api/communities/:cid/reports/:rid/restore` | 审核/admin | `{version_id,reason}` 恢复 |

### 课程

| 方法 | 路径 | 角色 | 说明 |
|---|---|---|---|
| POST | `/api/communities/:cid/courses` | admin/作者 | `{title}` |
| GET  | `/api/communities/:cid/courses` | 已认证 | 列表 |
| GET  | `/api/communities/:cid/courses/:courseID` | 管理/作者 | 草稿结构 |
| PUT  | `/api/communities/:cid/courses/:courseID/structure` | admin/作者 | 全量替换草稿结构 |
| POST | `/api/communities/:cid/courses/:courseID/publish` | admin/作者 | **全量校验 + 冻结快照** |
| POST | `/api/communities/:cid/courses/:courseID/unpublish` | admin | 回到草稿 |
| GET  | `/api/communities/:cid/courses/:courseID/published` | 已认证（逐课时门禁） | 冻结快照 |

结构请求体（position 决定模块与课时顺序）：
```json
{ "modules": [ { "position": 1, "title": "M",
  "lessons": [ { "position": 1, "title": "L", "content_version_id": 12 } ] } ] }
```

### 错误格式

`400` 校验错误、`401` 未认证、`403` 越权（含自审/审查员续费/层级不足/过期）、
`404` 不存在或跨社区、`409` 状态冲突（幂等冲突、审核版本失效、发布校验失败、
申诉已用过等）。响应体统一为 `{"error": "..."}`。

---

## 端到端示例

```bash
BASE=http://localhost:8080
# 1. 登录（示例数据）
TOKEN=$(curl -s $BASE/api/login -d '{"email":"admin@demo.local","password":"demo1234"}' | jq -r .token)
# 2. 层级列表
curl -s -H "Authorization: Bearer $TOKEN" $BASE/api/communities/1/tiers | jq
# 3. 登记收款（注意 X-Request-Id）
curl -s -X POST $BASE/api/communities/1/payments \
  -H "Authorization: Bearer $TOKEN" -H 'X-Request-Id: 7f3f...' \
  -d '{"user_id":4,"tier_id":2,"amount_cents":2900,"extend_days":90}'
```

## 目录

```
cmd/server        HTTP 服务入口（启动时自动迁移）
cmd/seed          示例数据
db/queries        sqlc 查询（按域分文件）
migrations        SQL 迁移（源；构建时嵌入 internal/database/migrations_sql）
internal/database pgx 连接池、事务封装、嵌入式迁移
internal/database/sqlcgen  生成代码（勿手改）
internal/services 业务规则与事务边界
internal/handlers  Chi 路由与 HTTP 适配
internal/middleware JWT 认证、请求 ID
scripts/run-tests.sh  真实数据库测试
```
