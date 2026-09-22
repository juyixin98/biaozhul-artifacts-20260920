# ActivityGuard — 员工活动异常检测后端

使用 **Go + Gin + GORM + MySQL 8 + Docker Compose** 实现的员工活动异常检测服务。
所有数据只在本机处理：数据库仅绑定 `127.0.0.1`，镜像与代码不包含任何外发数据逻辑。

> 本系统仅用于安全合规检测（异常登录、批量下载、USB 使用、夜间活动），
> **不做、也不提供任何个人绩效评分功能**。

## 功能总览

- **事件批量接入**：每批最多 2000 条，类型为 `login` / `file_download` / `usb`。
  - `event_id` 幂等：重复上报相同内容返回 `duplicate`，不重复入账；
  - 同 `event_id` 不同内容返回 **409 Conflict**，整批原子拒绝；
  - 支持乱序、延迟上报。允许补算窗口默认 `[现在-30天, 现在+5分钟]`（可调）。
- **检测规则**
  - 固定规则：10 分钟内下载超过 50 个文件、员工首次 USB、员工所属时区本地 20:00–次日 06:00 夜间活动；
  - 统计规则：以最近 30 个历史自然日（按员工时区）每日下载量为样本，当日计数超过均值 **2.5 个标准差**；
    样本不足（少于 7 天历史或少于 5 个有事件日）时自动停用，只用固定规则；
  - 跨日与夏令时边界按 IANA 时区在服务端精确换算。
- **告警生命周期**：`new → investigating → escalated → resolved / false_positive`（含合法回退）；
  `new` 状态 24 小时未确认由定时任务自动升级；任何补算只重算受影响窗口，
  以唯一 `dedup_key` upsert，**不产生重复告警**，且不覆盖人工状态；
  告警固化 `rule_version` 与完整 `evidence`（告警依据）。
- **权限模型**
  - 分析员（analyst）只能查看被分配部门的员工、事件衍生告警，越权访问返回 404（不泄露资源存在性）；
  - 管理员（admin）可发布规则新版本、修改员工时区、查看审计日志；
  - 调查状态变更、规则版本发布、时区修改全部写审计日志。
- **可靠性**
  - 事件“先落库（`detected_at=NULL`）后检测”，进程重启不丢待处理事件；启动恢复 + 每分钟安全网扫描；
  - 调度任务用数据库行锁单实例化（带 TTL、心跳），重复执行/多实例均幂等。

## 目录结构

```
cmd/server         API 服务入口
cmd/seed-events    本地演示数据工具（推送样例文件 / 生成突增与基线数据）
migrations         SQL 迁移（内嵌进二进制，启动自动执行）
internal/api       HTTP 路由与处理器
internal/detection 检测引擎（固定规则 + z-score、窗口重算、告警 upsert）
internal/scheduler 24h 自动升级、待处理事件恢复（行锁 + 幂等）
internal/models    GORM 模型
internal/auth      JWT 与 bcrypt
internal/seed      首启幂等种子（部门/员工/账号/规则）
samples            样例事件 JSON
tests/integration  MySQL 端到端测试（并发导入、乱序补算、时区边界、重复调度、部门越权）
```

## 快速开始（Docker Compose）

前置：Docker 与 Docker Compose 插件。

```bash
cp .env.example .env          # 按需修改密码 / 端口
docker compose up -d --build  # 启动 MySQL 与 API；自动迁移 + 写入种子数据
curl -s http://127.0.0.1:8080/healthz
```

若本机 8080/3306 已被占用：

```bash
MYSQL_PORT=3307 API_PORT=18092 docker compose up -d --build
```

预置账号（请在生产环境通过 `SEED_ADMIN_PASSWORD` / `SEED_ANALYST_PASSWORD` 覆盖）：

| 用户名        | 角色    | 密码           | 可见部门                 |
|---------------|---------|----------------|--------------------------|
| `admin`       | admin   | `admin12345`   | 全部                     |
| `analyst_eng` | analyst | `analyst12345` | Engineering              |
| `analyst_sales` | analyst | `analyst12345` | Sales, Finance           |

### 本地直接运行（不用容器跑 API）

```bash
# 只启动 MySQL
MYSQL_PORT=3307 docker compose up -d mysql

export DB_HOST=127.0.0.1 DB_PORT=3307 \
       DB_USER=guard DB_PASSWORD=guardpw_change_me DB_NAME=activityguard
go run ./cmd/server
```

## 使用流程

### 1. 登录

```bash
TOKEN=$(curl -s -X POST http://127.0.0.1:8080/api/v1/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"username":"admin","password":"admin12345"}' | jq -r .access_token)
```

### 2. 推送样例事件

```bash
# 样例文件：夜间活动、首次 USB、相对当前时间的延迟事件
API_URL=http://127.0.0.1:8080 go run ./cmd/seed-events file samples/events.sample.json

# 生成“同一 10 分钟桶内 51 个下载”（触发 download_burst）
API_URL=http://127.0.0.1:8080 go run ./cmd/seed-events gen --mode burst \
  --employee-email bob.li@example.com --count 51

# 生成 29 天基线 + 当日突增（样本充足后触发 zscore_activity）
API_URL=http://127.0.0.1:8080 go run ./cmd/seed-events gen --mode zscore \
  --employee-email bob.li@example.com --baseline 3 --today 20 --days 29
```

样例文件的 `occurred_at` 支持三种写法：

- RFC3339：`2026-09-20T13:00:00Z`
- 相对当前：`now-15m`、`now-2h`（Go duration）
- 按员工时区解释的本地时间：`local:2026-09-19T21:30:00`

> 注意：包含 `now-*` 相对时间的批次重放时时间戳已变化，会按设计返回 409 冲突；
> 验证幂等请使用固定 RFC3339 时间。

### 3. 直接调用批量接口

```bash
curl -s -X POST http://127.0.0.1:8080/api/v1/events/batch \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' -d '{
  "events": [{
    "event_id": "evt-1001",
    "event_type": "file_download",
    "employee_id": "<员工ID，见 GET /api/v1/employees>",
    "occurred_at": "2026-09-20T18:25:00Z",
    "metadata": {"file": "q3_plan.pdf", "size_bytes": 845213}
  }]}'
```

响应：

```json
{
  "accepted": true,
  "accepted_count": 1,
  "duplicate_count": 0,
  "detected": true,
  "items": [{"event_id": "evt-1001", "status": "accepted", "occurred_at": "..."}],
  "backfill_window": {"oldest_allowed": "...", "newest_allowed": "..."}
}
```

### 4. 查看与处理告警

```bash
# 列表（支持 status / rule_key / employee_id / department_id / limit / offset）
curl -s "http://127.0.0.1:8080/api/v1/alerts?status=new" -H "Authorization: Bearer $TOKEN" | jq

# 详情（含 evidence 告警依据与规则版本）
curl -s http://127.0.0.1:8080/api/v1/alerts/<id> -H "Authorization: Bearer $TOKEN" | jq

# 状态流转：investigating / escalated / resolved / false_positive
curl -s -X POST http://127.0.0.1:8080/api/v1/alerts/<id>/transition \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"status":"investigating","note":"已联系员工确认"}'
```

合法状态流转：

```
new           -> investigating | escalated | resolved | false_positive
investigating -> escalated | resolved | false_positive
escalated     -> investigating | resolved | false_positive
resolved / false_positive -> investigating（重新打开）
```

### 5. 管理员：规则版本与员工时区

```bash
# 调整下载突增阈值（发布 v2，旧版本自动停用；历史告警保留 v1 依据）
curl -s -X POST http://127.0.0.1:8080/api/v1/rules/download_burst/versions \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"params":{"window_minutes":10,"threshold":30}}'

# 修改员工时区（IANA 名称）
curl -s -X POST http://127.0.0.1:8080/api/v1/employees/<id>/timezone \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"timezone":"Europe/Berlin"}'

# 审计日志（调查、规则、时区变更）
curl -s http://127.0.0.1:8080/api/v1/audit-logs -H "Authorization: Bearer $TOKEN" | jq
```

## 补算时间范围

- 事件 `occurred_at` 合法窗口：`[now - BACKFILL_WINDOW, now + FUTURE_SKEW]`；
  超出窗口整批 400 拒绝（响应逐项给出原因 `outside_backfill_window` / `future_event_beyond_skew`）。
- 默认 `BACKFILL_WINDOW=720h`（30 天）、`FUTURE_SKEW=5m`，在 `.env` 中调整。
- 迟到事件进入后，引擎只重算其受影响窗口（10 分钟桶 / 夜间标签日 / 本地自然日序列），
  告警按 `dedup_key` upsert：新告警不重复，依据被刷新，人工状态不被覆盖。

## 调度任务

| 任务               | 周期     | 锁名               | 说明                                   |
|--------------------|----------|--------------------|----------------------------------------|
| `auto_escalation`  | 每分钟   | `auto_escalation`  | `new` 且创建超 24h 未确认 → `escalated` |
| `pending_recovery` | 每分钟   | `pending_recovery` | 捡取 `detected_at IS NULL` 的事件补算   |

行锁带 TTL 与心跳，持锁进程宕机后其他实例可接管；任务全部幂等，可安全重复执行。
可用 `SCHEDULER_ENABLED=false` 关闭（测试/本地排障）。

## 测试

纯单元测试（无时区/DST、统计、哈希）无需数据库：

```bash
go test ./internal/...
```

端到端集成测试需要 MySQL（自动建库/删库）：

```bash
MYSQL_PORT=3307 docker compose up -d mysql
AG_RUN_INTEGRATION=1 \
AG_TEST_ADMIN_DSN="root:rootpw_change_me@tcp(127.0.0.1:3307)/?charset=utf8mb4&parseTime=true&loc=UTC" \
go test -p 1 -count=1 ./tests/...
```

覆盖：并发导入幂等、同 ID 异内容 409 且批次原子、乱序补算不重复告警、
跨日与纽约 DST 边界、样本不足只走固定规则、重启待处理恢复、重复调度锁互斥、
24h 自动升级与审计、分析员部门越权（列表/详情/流转/规则/时区）。

## 迁移

- SQL 文件位于 `migrations/`，启动时按文件名顺序自动执行（`CREATE TABLE IF NOT EXISTS`，可重复执行）；
- 迁移同时内嵌于二进制（`migrations/embed.go`），容器内也保留一份便于检查；
- 后续新增变更请添加 `0002_xxx.sql`。

## 停止与清理

```bash
docker compose down          # 停止并删除容器，保留数据卷
docker compose down -v       # 同时删除数据库卷（清空全部本地数据）
```
