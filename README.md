# ProofCycle — 包装打样审查后端

包装（Packaging）打样文件的版本化审查与签核后端。技术栈：**Go + Gin + GORM + MySQL**，本地文件存储，提供 Docker 一键启动。

仅支持本地上传的 **PDF / PNG**；不做 OCR、不做法规自动判定、不提供仪表板。

## 业务规则

- 作业包含 **1 名设计师、1 名项目经理、1–8 名审查员**；成员互不相同。
- 上传校验：大小上限、**按文件头（Magic Number）** 识别 PDF/PNG、**SHA-256**（可由客户端声明强校验，否则服务端计算）。
- 存储路径由服务端生成（`jobs/<jobId>/vN-<随机>.<ext>`），原始文件名只入库展示，**任何 `..`/绝对路径都被拒绝**。
- **修订文件永不覆盖**：逐版本新增；每个版本绑定独立的**清单快照**。
- 落盘/数据库一致性：内容先写 `tmp` 临时文件并校验 → 事务内 `rename` 原子落盘 → 写库；写库失败回滚并**补偿删除已落盘文件**；进程启动时 `SweepTemp` 清理中断残留。
- 检查项结论只有 `pass` / `fail` / `na`，**`fail` 必填原因**；必须覆盖快照全部条目。
- 提交新修订后：旧版本意见与历史**保留**，但作业回到待审查，**旧意见不能作为新版本批准依据**。
- 审查员只能提交**自己**的意见；**设计师不能批准自己的作业**；只有该作业 PM 可签核。
- 意见更新携带 `expected_version`（`row_version` 乐观锁），冲突明确返回 **409**。
- 最终签核须 **所有指定审查员完成且无失败项/未处理项**；作业级 `SELECT … FOR UPDATE` 行锁 + 版本唯一约束，保证并发签核、改意见、传修订都无法绕过检查。
- 按版本查询审查历史与导出报告（JSON / Markdown），报告含**文件摘要、清单、各审查员结论、签核依据**。
- 非成员访问作业统一返回 **404**，不泄露未授权作业的存在。

## 快速开始（Docker）

```bash
cp .env.example .env        # 如 8080/3306 被占用，修改 APP_PORT/DB_PORT
docker compose up -d --build
curl -s http://127.0.0.1:${APP_PORT:-8080}/healthz
```

compose 项目名固定为 `proofcycle`（卷名带前缀，不会与其他项目冲突）。
MySQL 首次启动会自动执行 `migrations/001_init.up.sql`，应用启动也会 AutoMigrate 并写入演示数据（可用 `PROOFCYCLE_SEED=false` 关闭）。

### 演示

```bash
BASE_URL=http://127.0.0.1:${APP_PORT:-8080} ./scripts/demo.sh
```

演示流程：建作业 → 上传首版 → 失败项拦截签核 → 提交修订 v2 → 全员通过 → 角色隔离/并发签核 → 历史查询 → 报告导出。

## 本地运行（无 Docker）

```bash
# 1) 准备 MySQL 8.x，并执行结构迁移
mysql -h127.0.0.1 -uroot -p proofcycle < migrations/001_init.up.sql

# 2) 启动
PROOFCYCLE_AUTO_MIGRATE=false \
MYSQL_HOST=127.0.0.1 MYSQL_DATABASE=proofcycle MYSQL_USER=root MYSQL_PASSWORD=*** \
PROOFCYCLE_STORAGE_ROOT=./data/files \
go run ./cmd/server
```

| 环境变量 | 默认值 | 说明 |
|---|---|---|
| `PROOFCYCLE_HTTP_ADDR` | `:8080` | 监听地址 |
| `PROOFCYCLE_STORAGE_ROOT` | `/data/files` | 本地文件存储根目录 |
| `PROOFCYCLE_MAX_UPLOAD_BYTES` | `20971520` (20MiB) | 单文件大小上限 |
| `PROOFCYCLE_AUTO_MIGRATE` | `true` | 启动时 AutoMigrate |
| `PROOFCYCLE_SEED` | `true` | 启动时写入演示用户/清单 |
| `PROOFCYCLE_MYSQL_DSN` | 由 `MYSQL_*` 拼装 | 完整 GORM DSN，设置后优先 |
| `MYSQL_HOST/PORT/DATABASE/USER/PASSWORD` | 127.0.0.1/3306/proofcycle/… | DSN 拼装变量 |

## 认证（演示）

所有 `/api/v1/*` 请求带头 `X-User-Id: <用户ID>`，服务校验用户存在并据此做角色/成员隔离。
种子用户：`u-designer`（设计师）、`u-pm`（项目经理）、`u-reviewer-1 … 8`（审查员），默认清单 `cl-packaging-default`（8 项）。生产环境应替换为正式鉴权中间件。

## API 一览

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/healthz` | 健康检查 |
| GET | `/api/v1/users` | 用户列表 |
| POST | `/api/v1/jobs` | PM 建作业（必须以自己为 PM） |
| GET | `/api/v1/jobs` | 我参与的作业（非成员不可见） |
| GET | `/api/v1/jobs/:jobId` | 作业详情（非成员 404） |
| POST | `/api/v1/jobs/:jobId/versions` | 设计师上传首版/修订（multipart：`file`、可选 `sha256`） |
| GET | `/api/v1/jobs/:jobId/versions` | 版本列表 |
| GET | `/api/v1/jobs/:jobId/versions/:n/history` | 按版本审查历史 |
| GET | `/api/v1/jobs/:jobId/versions/:n/report?format=json\|md` | 报告导出 |
| GET | `/api/v1/jobs/:jobId/versions/:n/download` | 下载该版本文件 |
| POST | `/api/v1/jobs/:jobId/reviews` | 审查员提交/重交意见 |
| PUT | `/api/v1/jobs/:jobId/reviews` | 携带 `expected_version` 乐观更新（冲突 409） |
| POST | `/api/v1/jobs/:jobId/approvals` | PM 最终签核 |

审查意见请求体示例：

```json
{
  "version_no": 1,
  "items": [
    {"snapshot_item_id": "…", "result": "pass"},
    {"snapshot_item_id": "…", "result": "fail", "fail_reason": "条码扫读等级仅 D"}
  ]
}
```

PUT 更新额外带 `"expected_version": 1`。

错误映射：校验问题 `400`、未认证 `401`、越权 `403`、冲突/业务拦截 `409`（或 `403`）、不存在/未授权资源 `404`。

## 数据模型与迁移

- 结构实体见 `internal/domain/models.go`；SQL 迁移见 `migrations/001_init.up.sql`（及 `.down.sql`），由实际 GORM AutoMigrate 结果导出，已在全新 MySQL 8.4 上验证。
- 关键唯一约束：
  - `file_versions (job_id, version_no)`、`(job_id, superseded_by_id)`；
  - `reviews (version_id, reviewer_id)`；
  - `review_items (review_id, snapshot_item_id)`；
  - `approvals (job_id, version_id)` —— 并发双签的数据库兜底。

## 测试

```bash
# 需要一个可连接的 MySQL（仅集成测试）；无 MySQL 时集成测试自动 skip
docker run -d --name proofcycle-mysql-test -p 13390:3306 \
  -e MYSQL_ROOT_PASSWORD=rootpw mysql:8.4 --innodb-use-native-aio=0

go test ./...                       # 存储层单测 + MySQL 集成测试
go test -race ./...                 # 含并发签核/并发上传竞态检测
```

集成测试为每个用例创建独立数据库、结束自动删除，覆盖：

- 上传中断临时文件清理、**落盘后 DB 失败的补偿删除**；
- 文件头/大小/SHA-256 校验、**路径越界**；
- 修订不覆盖、**新版本旧意见不作数**、版本失效；
- 失败项拦截、未处理/未齐拦截、`fail` 必填原因；
- **并发签核只成功一次**、乐观锁冲突 409；
- 审查员只能提交本人意见、**设计师不能批准自己作业**、非成员 404 的角色隔离；
- multipart 上传/下载、报告导出。

## 目录结构

```
cmd/server         服务入口
internal/config    环境变量配置
internal/domain    GORM 实体与枚举
internal/database  连接与 AutoMigrate
internal/storage   本地文件存储（原子写/校验/越界防护/清理）
internal/service   业务规则（作业/版本/意见/签核/报告）
internal/httpapi   Gin 路由与处理器
internal/seed      演示用户与默认清单
migrations         SQL 迁移（up/down）
scripts/demo.sh    端到端演示
testdata           示例 PDF/PNG
integration        MySQL 集成测试
```
