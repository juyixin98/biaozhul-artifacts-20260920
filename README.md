# FieldSnap 离线表单同步后端

面向现场表单采集的离线优先同步服务：版本化表单模板、批量离线提交（幂等/冲突保留）、
主管显式冲突解决、稳定游标增量拉取（含删除标记）。基于 **Django REST Framework + MySQL + Docker Compose**。

> 范围限定：只实现现场表单，不含健康、运动、饮食模块。

---

## 1. 快速启动

### 方式 A：Docker Compose（MySQL，推荐）

```bash
cp .env.example .env          # 按需修改数据库口令/SECRET_KEY/WEB_PORT
docker compose up -d --build
docker compose logs -f web    # 等待 “Listening at ...”
```

compose 项目名固定为 `fieldsnap-text28b`（避免与其他同目录名工程冲突），
宿主机端口可用 `WEB_PORT`（默认 8000；被占用时例如 `WEB_PORT=18028 docker compose up -d`）。

启动后：

- API 根地址： <http://localhost:8000/api/>
- 管理后台： <http://localhost:8000/admin/>
- 容器启动时自动执行迁移；`DJANGO_SEED_DEMO=1` 时写入演示数据。

演示账号（密码均为 `fieldsnap123`）：

| 账号 | 角色 | 说明 |
|---|---|---|
| `admin` | 管理员 | 全项目；管理项目/班组/模板 |
| `sup_north` | 主管 | 北区班组主管；发布该项目模板、解决冲突、删除记录 |
| `w1` / `w2` | 工作人员 | 仅被分配到北区项目，可推送/拉取 |

### 方式 B：本地直接运行（SQLite，无需 Docker）

```bash
pip install -r requirements.txt
python manage.py migrate
python manage.py seed            # 可选：演示数据
python manage.py runserver
```

### 方式 C：本地连 MySQL

```bash
export USE_SQLITE=0 DB_HOST=127.0.0.1 DB_PORT=3306 \
       DB_NAME=fieldsnap DB_USER=fieldsnap DB_PASSWORD=fieldsnap_pass
python manage.py migrate
python manage.py runserver
```

### 运行测试

```bash
# SQLite（快速，默认）
python manage.py test core

# MySQL（容器内，含真实行锁并行用例）
# 注：应用账号默认仅有业务库权限，跑测试前由 root 授权一次：
#   docker compose exec db mysql -uroot -p"$DB_ROOT_PASSWORD" -e \
#   "GRANT ALL ON \`test_fieldsnap\`.* TO 'fieldsnap'@'%'; FLUSH PRIVILEGES;"
docker compose run --rm -e USE_SQLITE=0 -e SYNC_HWM_ID_GRACE=0 \
    web python manage.py test core -v 2
```

> `SYNC_HWM_ID_GRACE` 是增量拉取高水位安全余量（生产默认 100，测试设 0）。

---

## 2. 数据模型与核心概念

```
Project 项目 ──< Team 班组（多对多：projects/supervisors/members）
     │
     ├──< FormTemplate 表单模板（code + version 版本化，草稿/已发布/已弃用）
     │        └ schema = {fields:[{key,label,type,required,required_if,min,max,options}]}
     │
     ├──< FormRecord 现场记录（客户端生成 UUID，项目内唯一）
     │        ├──< RecordVersion 记录版本（只追加，content_hash 去重）
     │        └── Conflict 冲突（open/resolved，winning_version + resolution_note）
     │
     ├──< ChangeLog 变更事件（BIGINT 单调主键 = 同步游标）
     ├──< SyncBatch/SyncItem 推送批次与条目级结果（断网对账）
     └──< SyncState 各用户的拉取游标（服务重启续传）
```

- **字段类型**：`text`、`number`（min/max）、`enum`（options）、`date`（YYYY-MM-DD）。
- **条件必填**：`required_if: {field, op: eq|ne|in|not_in, value}`。
- **每表最多 150 个字段**（`TEMPLATE_MAX_FIELDS`），**每批最多 50 条**（`SYNC_MAX_BATCH_SIZE`）。
- **发布不可变**：已发布/已弃用模板行除状态外不能修改；历史提交始终携带
  `template_version`，并**按当时模板版本校验和读取**。

---

## 3. 同步语义（重点）

### 3.1 批量推送（离线填写后同步）

`POST /api/projects/{project_id}/sync/push/`

每条携带：`uuid`、`template_code`、`template_version`、`record_version`
（客户端记录版本号，整数，从 1 开始，仅作为该设备上的版本标签与审计信息）、
`collected_at`（采集时间）、`data`。
整批携带幂等键 `client_batch_id`。

条目结果状态：

| status | 含义 |
|---|---|
| `created` | 新记录创建 |
| `idempotent` | 同 UUID **同内容**重试，返回首次结果（不产生新版本） |
| `conflict` | 同 UUID **不同内容**，双方版本都保留，等待主管解决，**绝不覆盖** |
| `error` | 校验/权限/模板错误（附带 `code` 与字段级 `detail`），不影响同批其他条目 |

- 整批重试（同 `client_batch_id`）返回首次持久化的条目级结果，`replayed: true`。
- 即使新提交的 `record_version` 标签不同，只要内容哈希一致也判为幂等重放。
- **冲突只看内容**：任何同 UUID 不同内容的提交都生成候选版本并标记冲突，
  即使其 `record_version` 标签落后（离线设备过期），也不会自动覆盖或丢弃，
  统一由主管在冲突界面选择，保证"不能覆盖已有记录"。
- 冲突期间再提交已有的某一方内容，仍然是幂等（不产生第三个版本）。
- 批次条目级结果可通过 `GET /api/projects/{id}/sync/batch/?client_batch_id=...` 对账。
- 有任意条目 error 时返回 **HTTP 207**；批次整体不合法（>50 条/空）返回 400。
- 已删除记录再次提交返回 `record_deleted` 错误；有未解决冲突时不能删除记录。

### 3.2 冲突解决（保留双方版本，主管显式处理）

`POST /api/conflicts/{id}/resolve/`

```json
{ "winning_version_id": 12, "note": "电话核对后采用设备B，设备A为误填" }
```

- 只有**该项目所属班组的主管**（或管理员）能解决；工作人员 403。
- `note`（解决依据）必填（≥2 字）；落选版本标记为 `superseded`，**内容永久保留**可审计。
- 重复解决返回 409；只能选择该冲突内保留的版本。
- 解决后产生 `conflict_resolved` 增量事件，其他设备据此收敛。

### 3.3 增量拉取（稳定游标 + 删除标记）

`GET /api/projects/{id}/sync/pull/?cursor=&hwm=&limit=`

- 游标是不透明 BIGINT（即 `ChangeLog.id`），事件类型：
  `record_upserted` / `conflict_detected` / `conflict_resolved` / `record_deleted`（删除墓碑，`payload.deleted=true`）。
- **第一页**服务端计算高水位 `hwm = max(id) - SYNC_HWM_ID_GRACE`（默认余量 100，
  用于吸收 MySQL InnoDB「低 id 事务晚提交」的 id 空洞）；**后续页必须原样回传 hwm**，
  整轮读取固定快照 `(cursor, hwm]`。因此**翻页期间发生的新写入不会漏**：
  它们落在 hwm 之后，下一轮同步读到。
- `has_more=false` 表示该快照窗口消费完；下一轮不传 `hwm` 即获取新窗口。
- 游标按用户+项目保存在 `SyncState`：**服务重启后不带 cursor 继续拉取也不重不漏**。
- 每个事件携带发生时渲染的快照 `payload`，读取的就是提交当时的模板版本内容。
- 生产安全余量：保持默认 100（约两批条数的两倍）；若写入吞吐更高，按
  `SYNC_HWM_ID_GRACE` 环境变量调大。

### 3.4 模板升级兼容策略

新版本通过「基于旧版本派生草稿 → 修改 → 发布」完成，发布时做兼容性分析：

| 变更 | 策略 |
|---|---|
| 删除字段 | **允许**，自动加入 `legacy_fields` 白名单；旧设备仍可提交这些字段，数据原样保留，返回兼容告警 |
| 字段改类型 | **阻断发布**（旧数据语义无法保证），请新建字段 |
| 删除枚举选项 | **阻断发布**（旧设备可能仍提交该值） |
| 收紧数值范围 | **阻断发布** |
| 新增必填 / 新增条件必填 | **允许**，仅对新版本提交生效；旧版本提交按旧模板校验，不会失败或丢失 |
| 新版本发布 | 旧已发布版本自动置为「已弃用」，但**仍接受旧设备按旧版本号的提交** |

发布接口返回 `compatibility: {legacy_fields, warnings, blocking}` 作为变更审计。

---

## 4. 接口清单与示例

所有业务接口使用 Token 认证：`Authorization: Token <key>`
（`POST /api/auth/token/`，表单字段 `username`/`password` 换取）。

### 4.1 管理端（管理员）

```bash
# 项目
curl -X POST http://localhost:8000/api/projects/ -H "Authorization: Token $ADMIN" \
  -H 'Content-Type: application/json' -d '{"code":"NORTH-PJ","name":"北区项目"}'

# 班组（关联项目/主管/成员）
curl -X POST http://localhost:8000/api/teams/ -H "Authorization: Token $ADMIN" \
  -H 'Content-Type: application/json' \
  -d '{"code":"TEAM-A","name":"北一班","projects":[1],"supervisors":[2],"members":[3,4]}'
```

### 4.2 模板版本化（管理员 / 所属班组主管）

```bash
# 首版（创建后为草稿）
curl -X POST http://localhost:8000/api/templates/ -H "Authorization: Token $ADMIN" \
  -H 'Content-Type: application/json' -d '{
    "project": 1, "code": "site_check", "name": "现场巡检表",
    "schema": {"fields": [
      {"key":"site_name","label":"工地","type":"text","required":true},
      {"key":"phase","label":"阶段","type":"enum","options":["foundation","framing","finishing"]},
      {"key":"temp","label":"温度","type":"number","min":-50,"max":100},
      {"key":"day","label":"日期","type":"date","required":true},
      {"key":"issue_note","label":"异常说明","type":"text",
       "required_if":{"field":"phase","op":"eq","value":"finishing"}}
    ]}}'

# 发布草稿（幂等检查 + 兼容性报告）
curl -X POST http://localhost:8000/api/templates/1/publish/ -H "Authorization: Token $ADMIN"

# 基于已发布版本派生下一版草稿
curl -X POST http://localhost:8000/api/templates/1/new_draft/ -H "Authorization: Token $ADMIN" \
  -H 'Content-Type: application/json' -d '{"version_notes":"删除温度字段"}'
# PATCH /api/templates/2/ 修改草稿后再 publish

# 手动弃用某版本（一般由发布新版本自动完成）
curl -X POST http://localhost:8000/api/templates/1/deprecate/ -H "Authorization: Token $ADMIN"
```

### 4.3 工作人员：批量推送

```bash
curl -X POST http://localhost:8000/api/projects/1/sync/push/ \
  -H "Authorization: Token $W1" -H 'Content-Type: application/json' -d '{
    "client_batch_id": "device-A-20260921-001",
    "device_id": "device-A",
    "items": [{
      "uuid": "550e8400-e29b-41d4-a716-446655440000",
      "template_code": "site_check",
      "template_version": 1,
      "record_version": 1,
      "collected_at": "2026-09-21T08:30:00Z",
      "data": {"site_name":"1号井","phase":"foundation","temp":22,"day":"2026-09-21"}
    }]}'
```

响应（全部成功）：

```json
{
  "batch_id": 7,
  "client_batch_id": "device-A-20260921-001",
  "replayed": false,
  "item_count": 1,
  "results": [{
    "uuid": "550e8400-...", "status": "created",
    "code": "created", "message": "记录已创建",
    "record_version_id": 31
  }]
}
```

冲突响应（另一台设备提交了同 UUID 的不同内容）：

```json
{"status": "conflict", "code": "content_conflict",
 "message": "检测到同 UUID 的不同内容：双方版本均已保留，等待主管解决",
 "record_version_id": 32, "conflict_id": 5}
```

校验失败示例（HTTP 207，条目 `status=error`）：

```json
{"status":"error","code":"validation_failed","message":"未通过模板校验",
 "detail":{"issue_note":"必填（含条件必填）","temp":"不能大于 100"}}
```

断网后整批原样重发即可；不确定服务端是否已处理时，也可以先对账：

```bash
curl -H "Authorization: Token $W1" \
  "http://localhost:8000/api/projects/1/sync/batch/?client_batch_id=device-A-20260921-001"
```

### 4.4 增量拉取（翻页期间新写入不漏）

```bash
# 第一页（不传 cursor 时从服务端保存的位置续传）
curl -H "Authorization: Token $W1" \
  "http://localhost:8000/api/projects/1/sync/pull/?limit=100"
# -> {"changes":[...], "next_cursor": 104, "hwm": 200, "has_more": true, ...}

# 后续页：必须回传首页的 hwm
curl -H "Authorization: Token $W1" \
  "http://localhost:8000/api/projects/1/sync/pull/?cursor=104&hwm=200&limit=100"
# has_more=false 后，下一轮不传 hwm 开新窗口
```

删除事件：

```json
{"id":205,"kind":"record_deleted","record_uuid":"550e8400-...",
 "payload":{"deleted": true, "status":"deleted", "template_code":"site_check","template_version":1}}
```

### 4.5 冲突处理（主管）

```bash
# 待解决冲突列表（默认只看 open；?status=all 查看全部）
curl -H "Authorization: Token $SUP" "http://localhost:8000/api/conflicts/?project=1"

# 查看记录保留的全部版本
curl -H "Authorization: Token $SUP" "http://localhost:8000/api/records/<uuid>/"

# 解决（解决依据必填）
curl -X POST http://localhost:8000/api/conflicts/5/resolve/ \
  -H "Authorization: Token $SUP" -H 'Content-Type: application/json' \
  -d '{"winning_version_id":32,"note":"现场电话核对，设备B为修正数据，设备A误填"}'

# 删除记录（打墓碑）
curl -X POST http://localhost:8000/api/records/<uuid>/delete/ \
  -H "Authorization: Token $SUP"
```

### 4.6 权限边界

| 操作 | 工作人员 | 主管 | 管理员 |
|---|---|---|---|
| 推送/拉取 | 仅**被分配**的项目 | 所属班组关联项目 ∪ 分配项目 | 全部 |
| 项目/班组管理 | ✗ | ✗ | ✓ |
| 模板创建/发布 | ✗ | 仅**所属班组**关联项目 | 全部 |
| 冲突解决 / 删除记录 | ✗ | 仅**所属班组**关联项目 | 全部 |

`GET /api/me/` 可查看当前用户的角色、可见项目与主管班组。越权访问未分配项目返回 **403**。

---

## 5. 测试覆盖

`core/tests/` 共 50 个用例：

- `test_retry.py`：断网重试、同 UUID 同内容幂等返回原结果、批次条目级结果持久化/对账、
  50 条上限、单条错误不回滚整批（HTTP 207）。
- `test_concurrency.py`：并发不同内容双方版本保留且不覆盖、幂等重放、主管解决与
  解决依据必填、工作人员/跨班组主管越权、重复解决 409、冲突中禁止删除、删除后拒绝重提；
  另有 `TransactionTestCase` 真并行用例（MySQL 行锁，SQLite 自动跳过）。
- `test_template_upgrade.py`：发布版本不可变、150 字段上限、删字段进 legacy 白名单、
  改类型/删枚举/收紧范围阻断发布、收紧必填只对新版本生效、条件必填按版本校验。
- `test_incremental_pull.py`：游标翻页不重不漏、翻页途中新写入下一轮可见、
  墓碑删除事件、冲突/解决事件、服务重启按 SyncState 续传、历史模板版本快照、按项目隔离。
- `test_authorization.py`：未认证 401、未分配项目推送/拉取 403、项目列表按分配过滤、
  工作人员不能建项目/班组/删记录、主管不能碰其他班组数据。

---

## 6. 迁移与运维说明

```bash
python manage.py makemigrations core   # 模型变更后生成迁移（仓库已含 0001/0002）
python manage.py migrate               # 应用迁移（容器启动时自动执行）
python manage.py seed                  # 幂等写入演示账号/项目/班组/模板
python manage.py createsuperuser       # 自建管理员
```

- MySQL 使用 `utf8mb4`；时间统一 UTC（`USE_TZ=True`），客户端按 ISO 8601 传采集时间。
- 并发安全依赖 `SELECT ... FOR UPDATE`（推送按记录行加锁、批次按 `(user, client_batch_id)`
  唯一约束 + 行锁），请使用 InnoDB（MySQL 8 默认）。
- 生产部署时请修改 `.env` 中的口令与 `DJANGO_SECRET_KEY`，并将 `DJANGO_DEBUG=0`。
- 静态文件：`python manage.py collectstatic` 后交由 Nginx/gunicorn 托管。
