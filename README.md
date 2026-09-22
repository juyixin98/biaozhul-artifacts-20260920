# CommunityVault — 内容修订与审核引擎

一个可审计的社区内容修订与审核后端（**不含论坛前端**）。技术栈：**Go + Chi + sqlc +
PostgreSQL + Docker Compose**。

## 它解决什么问题

| 需求 | 实现方式 |
| --- | --- |
| 草稿/待审核/已发布/已撤回，正文每次修改生成新修订 | `contents.status` + 不可变 `content_revisions`（`revision_no` 单调递增） |
| 审核必须绑定**具体修订**与**具体规则版本**，旧批准不能覆盖新正文 | `reviews(revision_id, rule_version_id)`；只有 `draft` 可编辑，已提交/发布内容无法改字，只能先撤回 |
| 敏感词规则可版本化更新，发布时校验"最新生效规则" | `rule_versions` + `sensitive_words`；新版本先暂存、激活才生效 |
| 与规则切换并发时结果确定 | 提交/批准/激活共用同一把事务级咨询锁 `pg_advisory_xact_lock` + 可序列化/读已提交事务，批准时重新校验当前生效规则 |
| 驳回、撤回、回滚都保留原因 | `status_events.reason` 必填语义；回滚 `origin='rollback'` 并指向源修订，**生成新修订、不改历史** |
| 举报以（用户，内容）为唯一键，重复举报不加计数 | `reports UNIQUE (reporter_id, content_id)`，重复举报返回 `409 duplicate_report` |
| 审核队列并发领取、超时回收 | `FOR UPDATE SKIP LOCKED` 领取；`review_claims.expires_at`；后台 10s 回收 + 领取时同步回收 |
| 过期领取者不能覆盖后续处理结果 | 完成任务是带条件的单条 UPDATE（`claimant_id = $me AND expires_at > now() AND revision_id = $rev`），失败即 `409 stale_claim` |
| 成员只能改自己的内容；版主限所属分类 | 服务层鉴权 + `moderator_categories`；分类外任务对版主不可见 |
| 普通读取只能看到已发布修订 | 读侧只 JOIN `published_revision`；草稿/待审核/已撤回对非授权读返回 404 |
| 记录状态变更与审核证据，敏感信息按角色隐藏 | `status_events`（含 `rule_checked` 证据 JSON）、`reviews.matched_words`；作者看审计时隐藏审核员/举报人 ID |
| 稳定游标信息流，翻页期间新增/撤回不泄露 | keyset 分页 `WHERE id < $cursor ORDER BY id DESC`（不可变代理键），只查 `published` 并只 JOIN 已发布修订 |

## 目录结构

```
cmd/server/            入口（迁移、种子、HTTP、领取回收循环）
migrations/            4 个 SQL 迁移（用户/规则/内容修订/审核队列与审计）
queries/               sqlc 查询
internal/db/           sqlc 生成代码 + 事务助手（序列化失败自动重试）
internal/pgtypex/      jsonb 字符串数组类型
internal/content/      草稿/编辑/提交/撤回/回滚
internal/rules/        规则版本、激活、敏感词匹配
internal/moderation/   举报、领取、批准/驳回、回收
internal/view/         读侧可见性、角色脱敏、游标信息流
internal/httpserver/   Chi 路由与 Bearer 鉴权
internal/migrate/      极简 SQL 文件迁移器（写入 schema_migrations）
internal/integration/  并发/规则切换/重复举报/领取过期/权限集成测试
docker-compose.yml     postgres:16 + app
```

## 快速启动

要求：Docker + Docker Compose（本地不需要装 Go 即可运行容器；跑测试需要 Go 1.22+）。

```bash
docker compose up -d --build
# 数据库映射在宿主机 55440（避免与本机 5432 冲突），应用在 8080
curl -s localhost:8080/healthz
```

启动时应用会自动执行迁移并写入演示数据（仅当 users 表为空）。可用账号：

| 用户 | 角色 | Token | 备注 |
| --- | --- | --- | --- |
| alice / bob | member | `token-alice` / `token-bob` | 作者 |
| mod_tech | moderator | `token-mod-tech` | 只管分类 tech(id=1) |
| mod_life | moderator | `token-mod-life` | 只管分类 life(id=2) |
| root | admin | `token-admin` | 全部分类 + 规则管理 |

本地直接运行：

```bash
cp .env.example .env
docker compose up -d db
go run ./cmd/server
```

环境变量：`DATABASE_URL`、`ADDR`、`CLAIM_TTL`（领取有效期，默认 2m）、
`RUN_MIGRATIONS`、`RUN_SEED`、`MIGRATIONS_PATH`、`SEED_PATH`。

## 重新生成 sqlc 代码

```bash
sqlc generate   # 需要 sqlc v1.25+
```

## API 摘要

鉴权：`Authorization: Bearer <token>`（除 feed/categories/healthz 外都需要登录；
无效 token 按匿名处理，受保护端点返回 401）。

```
GET   /v1/feed?cursor=&limit=            公开，稳定游标
GET   /v1/categories                     公开
POST  /v1/contents/                      作者建草稿 {category_id,title,body}
GET   /v1/contents/{id}                  公开只读到已发布修订；作者/版主可读全文
PUT   /v1/contents/{id}                  仅作者、仅草稿；{body,reason} 生成新修订
POST  /v1/contents/{id}/submit           仅作者、仅草稿；命中敏感词返回 422
POST  /v1/contents/{id}/withdraw         作者或所属分类版主/管理员；{reason} 必填
POST  /v1/contents/{id}/rollback         仅作者；{revision_no,reason} 生成新修订
GET   /v1/contents/{id}/revisions        公开仅已发布修订；作者/版主见全部历史
POST  /v1/contents/{id}/reports          任意其他成员；{reason}；重复返回 409
GET   /v1/contents/{id}/events           作者/版主；作者视角 actor_id 被隐藏
GET   /v1/contents/{id}/reviews          作者/版主；作者视角 reviewer_id 被隐藏
GET   /v1/contents/{id}/reports          仅版主（限分类）/管理员
POST  /v1/moderation/claims              版主/管理员领取一个任务（SKIP LOCKED）
POST  /v1/moderation/claims/{taskID}/approve
POST  /v1/moderation/claims/{taskID}/reject   {reason}
POST  /v1/moderation/recycle             管理员手动回收过期领取
GET   /v1/moderation/reports             版主（限分类）/管理员
GET   /v1/rules/                         版主以上，列出全部版本
GET   /v1/rules/active                   版主以上，当前生效版本与词表
POST  /v1/rules/                         仅管理员，暂存新版本 {note,words[]}
POST  /v1/rules/{id}/activate            仅管理员，原子切换生效版本
```

错误响应统一为 `{"error":"<code>","message":"..."}`，典型 code：
`unauthorized / forbidden / not_found / conflict / validation_error /
sensitive_words / duplicate_report / no_task / stale_claim`。

## 完整审核示例（端到端）

```bash
B=http://localhost:8080

# 1) alice 写草稿（修订 1）
curl -s -X POST $B/v1/contents/ -H "Authorization: Bearer token-alice" \
  -d '{"category_id":1,"title":"Intro","body":"Hello CommunityVault"}'

# 2) 含敏感词（v1 词表含 spamword/evilco/forbidden-term）会被拒，停留在 draft
#    先创建 id=2 的脏草稿，再提交
curl -s -X POST $B/v1/contents/ -H "Authorization: Bearer token-alice" \
  -d '{"category_id":1,"title":"x","body":"buy spamword now"}'
curl -s -X POST $B/v1/contents/2/submit -H "Authorization: Bearer token-alice"
# -> 422 {"error":"sensitive_words",...}，同时写入 rule_checked 证据事件

# 3) 干净内容提交进队列
curl -s -X POST $B/v1/contents/1/submit -H "Authorization: Bearer token-alice"

# 4) 分类越权：life 版主领不到 tech 的任务
curl -s -X POST $B/v1/moderation/claims -H "Authorization: Bearer token-mod-life"
# -> 409 no_task

# 5) tech 版主领取（返回 task 与过期时间），并批准（审核绑定 revision 与当前规则）
curl -s -X POST $B/v1/moderation/claims -H "Authorization: Bearer token-mod-tech"
curl -s -X POST $B/v1/moderation/claims/1/approve \
  -H "Authorization: Bearer token-mod-tech"

# 6) 公开信息流可见
curl -s "$B/v1/feed"

# 7) bob 举报；再次举报返回 duplicate_report（计数绝不增加）
curl -s -X POST $B/v1/contents/1/reports -H "Authorization: Bearer token-bob" \
  -d '{"reason":"looks like spam"}'
curl -s -X POST $B/v1/contents/1/reports -H "Authorization: Bearer token-bob" \
  -d '{"reason":"again"}'                       # -> 409 duplicate_report

# 8) 版主带原因撤回；撤回后立即从公开信息流与公开读取消失
curl -s -X POST $B/v1/contents/1/withdraw -H "Authorization: Bearer token-mod-tech" \
  -d '{"reason":"policy update 2026-09"}'

# 9) 作者回滚到修订 1（生成修订 N，历史不动），需重新提交、重新审核
curl -s -X POST $B/v1/contents/1/rollback -H "Authorization: Bearer token-alice" \
  -d '{"revision_no":1,"reason":"restore original wording"}'

# 10) 审计：作者看不到审核员是谁；版主能看到 actor_id
curl -s $B/v1/contents/1/events -H "Authorization: Bearer token-alice"  # actor_id=null
curl -s $B/v1/contents/1/events -H "Authorization: Bearer token-mod-tech"
```

### 规则在审核期间切换（确定性结果）

```bash
# 内容已在 v1 下提交待审，管理员此时发布更严的 v2 并激活
curl -s -X POST $B/v1/rules/ -H "Authorization: Bearer token-admin" \
  -d '{"note":"ban reviewed-later","words":["reviewed-later"]}'
curl -s -X POST $B/v1/rules/2/activate -H "Authorization: Bearer token-admin"
# 版主再点"批准"：服务在批准事务内按最新生效规则重新校验，
# 命中新词 -> 自动记录一条 decision=rejected、rule_version_id=2 的审核，
# 内容回到 draft，绝不发布。
```

### 领取过期后旧领取者无法覆盖

领取有效期由 `CLAIM_TTL` 控制（默认 2m）。到期后：后台回收循环（10s）或下一次
`POST /claims` 会把任务重新置为 open 并删除旧 claim。旧版主随后再批准/驳回时，
条件 UPDATE 命中 0 行 -> `409 stale_claim`；任务可被他人重新领取并处理，旧处理
永远无法写入。

## 测试

集成测试使用真实 PostgreSQL（自动创建 `communityvault_test` 库并跑真实迁移）：

```bash
docker compose up -d db
go test ./... -count=1
go test ./internal/integration/ -race -count=3   # 并发压力
```

覆盖场景：

- **并发修改/批准**：可序列化事务 + 条件更新；12 轮"批准 vs 规则激活"竞速，
  结果只可能是两态之一（v1 已发布 / v2 驳回），不会出现状态与规则版本不一致。
- **规则切换**：暂存不生效；激活原子；提交与批准都在锁内读当前规则。
- **重复举报**：唯一键，二次返回 409，库内始终 1 行。
- **领取过期**：强制过期 → 回收 → 旧领取者 stale_claim → 他人重新领取处理后，
  旧领取者再次操作仍 stale_claim，最终发布结果不受影响。
- **8 路并发领取**：`SKIP LOCKED` 下每个任务只被领取一次。
- **权限**：非作者不能改；跨分类版主不能领取/决策/撤回；成员不能进队列；
  规则创建仅管理员；公开读取只见已发布修订；作者侧审计脱敏。
- **信息流**：翻页期间新增高 id 内容不串入旧页、撤回行从后续页消失、无重复、
  草稿/待审永不出现。

## 关键并发语义说明

1. **为什么编辑只允许 draft**：审核绑定修订 id。若允许在 pending 上继续改字，
   队列里的旧修订与正文就会失配；"先撤回/被驳回 → 改 → 重新提交"使每次发布都
   对应一次明确的修订级批准。
2. **规则锁**：`LockRuleAdvisory` 是单把事务级咨询锁，把"激活规则"与
   "按规则做发布判定"串行化，因此并发下的规则版本是全序确定的，而不是靠时间戳
   取巧。
3. **领取用 READ COMMITTED、其余写路径用 SERIALIZABLE**：领取的正确性完全由
   行锁（SKIP LOCKED）与条件更新保证，READ COMMITTED 下高并发无谓词锁冲突；
   序列化失败（40001/40P01）由 `db.InTx` 自动重试，业务函数每次都重读新状态。
4. **稳定游标**：以 contents.id（BIGSERIAL，不可变、单调）为键，新增只出现在比
   当前更新的位置、撤回只是后续页少一行，读者手中的旧页快照不受影响。
