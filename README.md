# MineRite — 流程一致性分析后端

基于 Django REST Framework + MySQL 的流程挖掘一致性检查服务：将案例事件日志与有向无环流程模板对照重放，产出带具体证据的偏差报告。本期范围仅包含事件日志与流程模板的对照分析，不含审批待办、预约或索赔平台。

## 快速启动（Docker）

```bash
docker compose up --build
```

`web` 服务会等待 MySQL 就绪、执行迁移，并（`LOAD_DEMO=1`）加载演示项目与演示事件日志，随后监听 `http://localhost:8000`。

运行测试（在 MySQL 上）：

```bash
docker compose --profile test run --rm test
```

本地开发（SQLite，无需 MySQL）：

```bash
pip install -r requirements.txt
DB_ENGINE=sqlite python manage.py migrate
DB_ENGINE=sqlite python manage.py test
DB_ENGINE=sqlite python manage.py load_demo
DB_ENGINE=sqlite python manage.py runserver
```

演示账号：`analyst / analyst123`（项目成员），`outsider / outsider123`（无权限，用于验证授权）。

获取令牌：

```bash
curl -X POST http://localhost:8000/api/token/ \
  -H 'Content-Type: application/json' \
  -d '{"username": "analyst", "password": "analyst123"}'
# 之后请求带  Authorization: Token <token>
```

## 流程模板

模板用 JSON 定义有向无环流程图，发布时校验合法性；已发布版本不可变，修改必须新建版本；每个案例绑定一个确定版本。

```json
{
  "activities": ["register", "review", "approve", "reject", "notify", "archive"],
  "dependencies": [
    {"from": "register", "to": "review"},
    {"any_of": ["approve", "reject"], "to": "notify"},
    {"from": "notify", "to": "archive"}
  ],
  "exclusive_groups": [["approve", "reject"]],
  "time_limits": [
    {"from": "register", "to": "review", "max_seconds": 172800},
    {"from": null, "to": "notify", "max_seconds": 604800}
  ]
}
```

- `dependencies`: `from` 为强制前置；`any_of` 为任一前置（用于互斥分支后的汇合）。
- `exclusive_groups`: 组内活动互斥，同一案例中出现两个即判偏差。
- `time_limits`: `from` 为锚点活动，`null` 表示从案例首个事件计时。
- 发布校验：活动唯一、引用存在、无自依赖、依赖图无环、互斥组/时限合法。

## 事件批量导入

`POST /api/projects/{id}/events/import/`，请求体 `{"events": [...]}`，每项：

```json
{
  "event_id": "e-1001-1",
  "case_id": "CASE-1001",
  "activity": "register",
  "occurred_at": "2026-09-01T09:00:00Z",
  "seq": 1,
  "template_version_id": 1
}
```

- `template_version_id` 仅在创建新案例时必需；已有案例若给出则必须与其绑定版本一致。
- **幂等**：相同 `event_id` 且内容完全一致的重复事件被跳过（`skipped` 计数），重复导入同一批是安全的。
- **冲突拒绝**：同 `event_id` 不同内容、或同案例同 `seq` 被不同事件占用（包括批内冲突），整批拒绝并回滚，一个事件都不会写入；失败批次留有错误明细。
- 允许乱序与迟到到达；受影响案例在导入后自动增量重算。

## 重放与偏差

按案例内 `seq` 重放，与模板对照。缺号（如 1,2,4 缺 3）时案例标记为 `waiting`，**不会**跳过缺失事件宣称完成。识别的偏差类型（均带 `evidence` 具体证据与 `constraint` 模板约束）：

| 类型 | 含义 |
|---|---|
| `missing_predecessor` | 活动执行时其必需前置尚未发生 |
| `duplicate_execution` | 无环模板中同一活动被执行多次 |
| `exclusive_violation` | 互斥组中两个活动都发生 |
| `timeout` | 超出 `time_limits` 时限 |
| `unknown_activity` | 事件活动未在模板中定义 |

案例状态：`waiting`（缺号）/ `in_progress`（无偏差未到终态）/ `conformant` / `nonconformant`。

迟到事件补齐后，受影响案例自动重算并产生新的分析修订（revision），旧结论保留为历史（`is_current=false`），可通过分析历史接口查询。增量分析与全量重建（`POST .../analysis/rebuild/`）结果一致；导入与重建通过对案例行加锁（`SELECT ... FOR UPDATE`）序列化，重建期间新导入的事件不会丢失。

## API 一览

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/token/` | 获取认证令牌 |
| GET | `/api/projects/` | 我被授权的项目列表 |
| GET/POST | `/api/projects/{id}/templates/` | 模板列表 / 创建模板（含草稿 v1） |
| GET/POST | `/api/templates/{id}/versions/` | 版本列表 / 新建草稿版本 |
| POST | `/api/versions/{id}/publish/` | 校验并发布（发布后不可变） |
| GET/POST | `/api/projects/{id}/cases/` | 案例列表 / 绑定案例到已发布版本 |
| POST | `/api/projects/{id}/events/import/` | 批量导入事件（原子、幂等） |
| GET | `/api/projects/{id}/cases/{key}/trace/` | 案例轨迹（按 seq 排序） |
| GET | `/api/projects/{id}/cases/{key}/deviations/` | 当前偏差（证据+约束） |
| GET | `/api/projects/{id}/cases/{key}/analysis-history/` | 分析修订历史 |
| GET | `/api/projects/{id}/reports/deviations/` | 项目偏差报告（逐条列明，可用 `?type=` 过滤，不做单一评分） |
| POST | `/api/projects/{id}/analysis/rebuild/` | 全量重建 |

授权：分析师只能访问其 `ProjectMembership` 授权的项目，其余一律 403。

## 测试

`conformance/tests/` 共 40 个用例，覆盖：乱序与迟到补齐、冲突整批回滚、幂等重复导入、互斥分支判断、缺少前置/重复执行/超时识别、模板校验与版本不可变、案例版本绑定、增量与全量重建一致、并发重建不丢事件、项目级授权。

## 目录结构

```
minerite/            Django 项目配置（DB_ENGINE=mysql/sqlite 切换）
conformance/         核心应用
  template_def.py    模板定义校验与编译
  models.py          项目/模板版本/案例/事件/批次/分析修订
  services.py        批量导入、重放分析、增量重算、全量重建
  views.py urls.py   REST API
  management/commands/load_demo.py   演示数据
  tests/             测试套件
demo/events.json     演示事件日志（两批：含迟到补齐）
docker/entrypoint.sh 等 MySQL 就绪 → 迁移 →（可选）演示数据 → 启动
Dockerfile docker-compose.yml
```
