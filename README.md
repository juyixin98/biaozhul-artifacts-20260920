# ProofCycle — 包装打样审查后端

ProofCycle 管理包装打样（印刷校样）的多版本审查与最终签核。技术栈：**Go + Gin + GORM + MySQL**，提供 **Docker / docker compose** 一键启动。

明确不做：不做 OCR、不做法规自动判定、不做仪表板；文件**仅接受本地 PDF 和 PNG**。

## 领域规则（与代码一一对应）

- **作业成员**：一名设计师、一名项目经理（PM）、**1–8 名**审查员；角色互斥（设计师不能同时是 PM 或审查员，PM 不能是审查员）。
- **文件上传**
  - 仅 PDF / PNG：校验魔数文件头（`%PDF-`、PNG 签名），并强制大小上限（默认 20 MiB）与 SHA-256 摘要。
  - 落盘路径完全由服务生成（`job-<id>/<随机名>.<ext>`），客户端文件名只存元数据；读取时统一做根目录逃逸检查，`../`、绝对路径、NUL 一律拒绝。
  - 修订是新文件新记录，**旧文件不可覆盖**。先落盘后写库，任一步失败都会清理；启动时清扫 `*.part` 中断文件与无库记录的孤儿 blob。
- **审查轮次**
  - 每轮绑定一个文件版本 + 一份**检查清单快照**（模板之后修改不影响历史轮次）。
  - 检查项结果只有 `pass` / `fail` / `na`；`fail` **必须填写原因**。
- **意见**
  - 审查员只能提交自己的意见（唯一键 `(round, reviewer, item)`）。
  - 更新必须携带 `expected_version`（乐观锁），版本不一致明确返回 **409**；不带版本的覆盖同样被拒绝。
  - 新修订提交后，旧轮次置为 `superseded`，旧意见保留可查，但**不能作为新版本的批准依据**。
- **签核**
  - 只有该作业的 **PM** 能签核；设计师永远不能批准自己的作业。
  - 存在未处理项 → 422；存在失败项 → 422；所有指定审查员未完成全部检查项 → 422。
  - 全部校验在数据库行锁 + 唯一约束内完成，**并发签核、并发改意见、并发提交修订都无法绕过**（最终只有一个签核成功）。
- **历史与报告**：按版本查询审查历史；报告包含文件摘要、清单快照、所有意见与冻结的签核依据。非作业成员访问任何资源都得到 **404**（不泄露作业是否存在）。

## 目录结构

```
cmd/server/             入口（迁移、种子、优雅关闭、孤儿清扫）
internal/config/        环境变量配置
internal/db/            GORM 连接 + 嵌入 SQL 迁移（migrations/*.sql）
internal/models/        GORM 模型
internal/storage/       本地文件存储（魔数/大小/SHA-256/路径安全/清扫）
internal/auth/          bcrypt + Bearer Token
internal/middleware/    认证与角色中间件
internal/service/       全部业务规则与事务
internal/api/           Gin 路由与 handler
internal/seed/          演示账号（幂等 upsert）
demo/demo.sh            端到端演示脚本
test/integration/       需要 MySQL 的集成/HTTP/并发测试
Dockerfile, docker-compose.yml, Makefile
```

## 快速开始（Docker）

```bash
docker compose up --build
# API: http://localhost:8080  MySQL: localhost:3306
```

compose 会等待 MySQL 健康后启动 API；API 启动时自动执行迁移并写入演示账号。

### 本地开发（不用 compose）

```bash
# 1) 起一个 MySQL
docker run -d --name proofcycle-mysql -p 23306:3306 \
  -e MYSQL_DATABASE=proofcycle -e MYSQL_USER=proof -e MYSQL_PASSWORD=proof \
  -e MYSQL_ROOT_PASSWORD=rootpw mysql:8.4

# 2) 跑服务（默认读取 127.0.0.1:23306）
make run MYSQL_PORT=23306 API_PORT=39071
# 或：go run ./cmd/server
```

## 配置（环境变量）

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `HTTP_ADDR` | `:8080` | 监听地址 |
| `MYSQL_DSN` | 由下面几项拼装 | 完整 DSN（需含 `multiStatements=true`） |
| `MYSQL_HOST/PORT/USER/PASSWORD/DATABASE` | `127.0.0.1 / 3306 / proof / proof / proofcycle` | 拼装 DSN 用 |
| `STORAGE_ROOT` | `./data/files` | 本地文件根目录（compose 内为卷） |
| `MAX_UPLOAD_BYTES` | `20971520` (20 MiB) | 单文件大小上限 |

## 演示账号

| 角色 | 用户名 | 密码 | 固定 Token（可直接用于脚本） |
| --- | --- | --- | --- |
| 设计师 | `dana` | `designer123` | `0000…0aa1`（64 位十六进制，见 `internal/seed/seed.go`） |
| PM | `priya` | `pm1234567` | `0000…0bb2` |
| 审查员 | `rev1` … `rev8` | `review123` | `0000…01c3` 等 |

> 调 `POST /api/v1/login` 会轮换该用户的 Token；重启服务会把演示 Token 刷回固定值。

## 端到端演示

```bash
make demo            # 默认 http://127.0.0.1:39071
BASE=http://127.0.0.1:8080 ./demo/demo.sh
```

脚本覆盖：建作业 → 上传校验 → 失败必填原因 → 非指派审查员拒绝 → 失败项拦截签核 →
设计师自签拒绝 → 乐观锁 409 → 修正后签核成功 → 报告导出 →
新修订使旧意见失效但保留历史 → 未授权用户 404。

## API 摘要

所有业务接口都需要 `Authorization: Bearer <token>`。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/v1/login` | 换发 Token |
| POST | `/api/v1/jobs` | PM/设计师创建作业（含 1–8 审查员） |
| GET | `/api/v1/jobs/:id` | 成员查看作业（非成员 404） |
| POST | `/api/v1/jobs/:id/versions` | 设计师/PM 上传首版（multipart `file`） |
| POST | `/api/v1/jobs/:id/revisions` | 提交新修订（旧轮次保留） |
| GET | `/api/v1/jobs/:id/versions` / `/:ver` | 版本列表 / 详情 |
| GET | `/api/v1/jobs/:id/versions/:ver/download` | 下载（带 `X-Content-SHA256`） |
| GET | `/api/v1/jobs/:id/rounds/active` | 当前轮次快照与意见 |
| GET | `/api/v1/jobs/:id/versions/:ver/history` | 按版本查历史 |
| GET | `/api/v1/jobs/:id/history` | 全部版本历史 |
| PUT | `/api/v1/jobs/:id/opinions` | 审查员批量提交/更新意见 |
| POST | `/api/v1/jobs/:id/approvals` | PM 最终签核 |
| GET | `/api/v1/jobs/:id/versions/:ver/report?format=text` | 报告导出（默认 JSON 包裹） |

意见提交体示例：

```json
{
  "items": [
    {"code": "BLEED", "outcome": "fail", "reason": "trim offset 2mm off", "expected_version": 1},
    {"code": "COLOR", "outcome": "pass", "expected_version": 3}
  ]
}
```

新建意见时 `expected_version` 传 `0`（或省略表示必须不存在）；更新已有意见必须传当前版本号。

## 测试

存储层单测无外部依赖：

```bash
go test ./internal/storage/
```

集成测试（service + HTTP + 并发）需要一个 MySQL，并会**重建** `proofcycle_test` 库中的表：

```bash
docker run -d --name proofcycle-mysql -p 23306:3306 \
  -e MYSQL_DATABASE=proofcycle -e MYSQL_USER=proof -e MYSQL_PASSWORD=proof \
  -e MYSQL_ROOT_PASSWORD=rootpw mysql:8.4
mysql -h127.0.0.1 -P23306 -uroot -prootpw \
  -e "CREATE DATABASE IF NOT EXISTS proofcycle_test; GRANT ALL ON proofcycle_test.* TO 'proof'@'%';"

make test MYSQL_PORT=23306        # 或 PROOFCYCLE_TEST_DSN=... go test ./...
```

测试覆盖（对应需求验收点）：

- **上传中断**：流中途断开不产生版本、不残留 blob/`.part`。
- **文件越界**：`../`、绝对路径、NUL 等全部被 `Resolve` 拒绝。
- **大小/文件头**：超限 → `ErrTooLarge`，非 PDF/PNG → `ErrUnsupportedKind`。
- **版本失效**：新修订后旧意见保留但无法批准新版本（`ErrPendingItems`）；清单快照冻结。
- **并发签核**：20 个并发签核只有 1 个成功；乐观锁 12 并发更新只有 1 个成功。
- **失败项拦截**：有 `fail` 或未完成项时 PM 无法签核（422）。
- **角色隔离**：审查员不能上传/建作业/签核，设计师不能自签，非成员访问一律 404。

## 安全说明

- 口令使用 bcrypt；Token 为 32 字节随机十六进制，仅经 HTTPS 部署时才安全（生产请在反向代理终止 TLS）。
- 服务端从不使用客户端提供的路径落盘；下载前做成员校验与根目录 containment 检查。
- 所有"是否可以签核"的判定均在 `REPEATABLE READ` 事务的行锁内重读数据，不存在 TOCTOU 窗口。
