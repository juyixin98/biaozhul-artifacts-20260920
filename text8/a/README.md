# 流程编排引擎

基于 JSON DSL 的轻量审批流引擎。技术栈 **FastAPI + SQLAlchemy 2 + PostgreSQL**，
不依赖任何消息队列或外部服务；超时升级由数据库表 + 进程内轮询线程驱动，
服务重启后可继续处理未完成的升级。

## 功能

- **JSON 流程定义**：`start` / `approval`（全签 `all`、任签 `any`）/ `condition`（条件分支）/ `end`
- **发布前强校验**：缺失引用、不可达节点、环路、重复节点、结构错误一律拒绝；
  条件为受限表达式（AST 白名单手工求值，**不使用 `eval`，禁止任意代码执行**）
- **版本不可变与隔离**：版本一经发布不可修改；新实例绑定当时的发布版本；
  修改模板（发新版本）或回滚只影响之后创建的实例，运行中的实例永远按绑定版本执行
- **幂等**：启动 / 审批 / 撤回均须携带 `request_id`，重复请求回放原结果；
  同 `request_id` 参数不一致返回 409 且不写入任何业务数据
- **并发安全**：实例行级锁串行化同一实例的状态转换，待办用条件 UPDATE 兜底，
  审批 / 撤回 / 超时升级只能产生一次有效转换；状态、待办、审计、升级标记同事务提交
- **全签 / 任签**：全签全部通过才推进，一票否决即拒绝；任签一人通过即推进并关闭其余待办；
  任签下有人拒绝但仍有待办时流程继续，全部拒绝才拒绝
- **撤回**：提交人在流程结束前可撤回，撤回后待办与超时升级一并关闭
- **越权防护**：只有待办指定审批人可操作；待办必须属于 URL 中的实例，替换实例 ID 无法越权
- **超时升级**：审批节点可配置超时秒数与升级目标，到期自动改派；任务失败可重试，重启后继续

## 快速开始（Docker）

```bash
docker compose up --build
```

启动后：

- API 服务：http://localhost:8000
- 交互式接口文档（Swagger）：http://localhost:8000/docs
- 健康检查：http://localhost:8000/health
- 首次启动自动执行数据库迁移，并写入演示模板 `leave-demo`（已发布 v1）

## 本地开发

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 需要一个 PostgreSQL，例如：
docker run -d --name flow-db -e POSTGRES_USER=flow -e POSTGRES_PASSWORD=flow \
  -e POSTGRES_DB=flow -p 5432:5432 postgres:16-alpine

export DATABASE_URL='postgresql+psycopg2://flow:flow@localhost:5432/flow'
alembic upgrade head
python -m scripts.seed_demo --publish      # 可选：写入演示模板
uvicorn app.main:app --reload
```

## 流程 DSL

```json
{
  "start_node": "start",
  "nodes": [
    {"id": "start", "type": "start", "next": "dept_approval"},
    {
      "id": "dept_approval", "type": "approval",
      "mode": "all",
      "assignees": ["alice", "bob"],
      "next": "amount_check",
      "timeout": {"seconds": 120, "targets": ["frank"]}
    },
    {
      "id": "amount_check", "type": "condition",
      "branches": [{"when": "amount >= 10000", "next": "gm_approval"}],
      "default": "end"
    },
    {
      "id": "gm_approval", "type": "approval",
      "mode": "any", "assignees": ["carol", "dave"], "next": "end"
    },
    {"id": "end", "type": "end"}
  ]
}
```

### 条件表达式规则（受限）

允许：字面量（字符串/数字/布尔/None/列表）、上下文变量、`and or not`、
`== != < <= > >= in / not in`、括号。

禁止（任何白名单外语法直接拒绝）：函数调用、属性访问、下标、算术运算、
lambda、推导式、赋值等。例如 `__import__('os').system('id')`、`x.f()`、
`x.__class__`、`x + 1` 都无法通过发布校验。变量取自实例 `context`，未定义为 `None`。

## API 摘要

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/templates` | 创建模板（含 v1 草稿，定义需通过校验） |
| GET | `/templates/{key}` | 模板与全部版本 |
| POST | `/templates/{key}/versions` | 新增草稿版本（不影响运行中实例） |
| POST | `/templates/{key}/versions/{v}/publish` | 发布版本（不可变），并切换当前指针 |
| POST | `/templates/{key}/rollback` | 回滚当前指针到已发布版本 `{"version":1}` |
| POST | `/instances` | 启动实例（body 含 `request_id`，绑定当前发布版本） |
| POST | `/instances/{id}/tasks/{task_id}/decision` | 审批/拒绝（`expected_version` + `request_id`） |
| POST | `/instances/{id}/withdraw` | 提交人撤回（`request_id`） |
| GET | `/instances/{id}` | 当前位置、状态、拒绝原因、待办 |
| GET | `/instances/{id}/history` | 审计历史（只追加） |
| GET | `/instances/{id}/tasks` | 全部待办及处理状态（可 `?status=` 过滤） |

完整字段见 `/docs`（OpenAPI）。

### 典型调用序列

```bash
# 1. 创建并发布模板
curl -X POST localhost:8000/templates -H 'Content-Type: application/json' \
  -d '{"key":"leave","name":"请假","definition": <demo/leave-demo.json 内容>}'
curl -X POST localhost:8000/templates/leave/versions/1/publish

# 2. 启动实例（request_id 由客户端生成并持久化）
curl -X POST localhost:8000/instances -H 'Content-Type: application/json' -d '{
  "template_key": "leave", "submitter": "zhang",
  "context": {"amount": 500}, "request_id": "uuid-1"
}'

# 3. 指定审批人携带预期版本决策
curl -X POST localhost:8000/instances/<id>/tasks/<task_id>/decision \
  -H 'Content-Type: application/json' -d '{
  "decision": "approve", "actor": "alice",
  "expected_version": 1, "request_id": "uuid-2"
}'
```

## 拒绝规则

- **全签**：任一人 `reject` 即节点不通过，流程 `rejected`，其余待办关闭，
  `reject_reason` 记录拒绝意见
- **任签**：一人 `approve` 即推进并关闭其他待办；某人 `reject` 后只要还有
  pending 待办流程继续；全部 `reject` 才使流程 `rejected`

## 并发与一致性说明

- 同一实例的所有写操作先 `SELECT ... FOR UPDATE` 锁定实例行，天然串行；
  不同实例互不阻塞
- 待办状态迁移使用 `UPDATE ... WHERE status='pending'` 条件更新，
  并发双击只有一行生效，败者得到 409
- 超时升级扫描使用 `FOR UPDATE SKIP LOCKED`，多副本部署不重复处理；
  节点已推进 / 流程已结束时到期升级被标记 `canceled`，不会产生第二次状态转换
- 实例状态、待办、审计事件、升级标记在**单个数据库事务**中提交，崩溃整体回滚
- 升级处理失败时仅记录 `attempts/last_error` 并保持 `pending`，下个轮询周期重试，
  服务重启后扫描线程立即捞起所有到期未完成记录

## 测试

需要 PostgreSQL（用 JSONB、行锁、SKIP LOCKED）：

```bash
export TEST_DATABASE_URL='postgresql+psycopg2://flow:flow@localhost:5432/flow'
pytest
```

47 个用例覆盖：

- `test_template_validation.py`：缺失引用 / 不可达 / 环路 / 危险表达式 / 结构错误
- `test_version_isolation.py`：发布不可变、新旧实例版本隔离、回滚、预期版本冲突
- `test_approval_flow.py`：全签、任签竞争、拒绝规则、并发双击、越权、撤回
- `test_idempotency.py`：启动 / 审批 / 撤回重放、指纹冲突不写历史
- `test_timeout_recovery.py`：到期改派、只生效一次、与审批竞争作废、重启恢复、失败重试
- `test_api.py`：端到端 HTTP 路径

## 配置

环境变量：

| 变量 | 默认值 | 说明 |
|---|---|---|
| `DATABASE_URL` | `postgresql+psycopg2://flow:flow@localhost:5432/flow` | 数据库 DSN |
| `WORKER_ENABLED` | `true` | 是否在进程内启动超时升级轮询线程 |
| `WORKER_POLL_SECONDS` | `2` | 轮询间隔 |
| `WORKER_BATCH_SIZE` | `20` | 每轮最大处理条数 |
| `SEED_DEMO` | `false`（compose 中为 `true`） | 启动时写入演示模板 |

## 目录结构

```
app/
  api.py          HTTP 路由
  engine.py       状态机/版本/幂等/审批/撤回/升级（核心）
  dsl.py          DSL 校验：引用、可达性、环路
  expressions.py  受限表达式（AST 白名单）
  models.py       ORM 模型
  worker.py       超时升级轮询线程
alembic/          数据库迁移
demo/             演示流程 JSON
scripts/          演示数据种子
tests/            测试
```

## 范围之外

不做表单设计器、电子签名、多业务模块接入、消息队列 / Webhook 通知。
