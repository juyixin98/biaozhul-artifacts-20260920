# RevStream — 广告瀑布流优化后端

仅处理**本地事件**的广告瀑布流（waterfall）优化服务：管理应用/广告位/广告网络、
版本化不可变瀑布流配置、幂等批量事件接收、30 分钟滚动评分、两版本 50/50 实验分流，
以及租户隔离与配置审计。**不调用任何广告网络 SDK / 服务端接口。**

技术栈：Django 4.2 + Django REST Framework 3.15 · MySQL 8 · Docker Compose ·
Gunicorn · 定点 Decimal 金额（`Decimal(14,6)`，全程不使用 float）。

---

## 1. 快速开始（Docker Compose）

```bash
cp .env.example .env          # 可选：调整端口/密码
docker compose up -d --build  # 启动 MySQL + Web + 评分调度器
```

`web` 容器启动时自动执行 `migrate`，并在 `LOAD_SAMPLE_DATA=1`（默认）时写入示例数据。

- API：<http://localhost:8000/api/v1/>
- MySQL：宿主机 `127.0.0.1:${MYSQL_PORT:-3325}`（host 网络模式，避免占用桥接子网）

示例账号（由 `seed_demo` 创建，见 `common/management/commands/seed_demo.py`）：

| 用途 | 值 |
| --- | --- |
| 开发者用户名 / 密码 | `demo` / `demo-pass123` |
| 广告位 key | `home_rewarded`（应用 `com.demo.revstream`） |
| Token / API Key | 启动日志中打印；也可通过下面的登录接口重新获取 |

停止 / 清理：

```bash
docker compose down            # 停止，保留数据卷
docker compose down -v         # 停止并删除数据库卷
```

### 本地不用 Docker 运行（SQLite）

所有数据访问均与数据库无关，无需 MySQL 也可跑完整套件：

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install "Django==4.2.16" "djangorestframework==3.15.2"
export REVSTREAM_DB_ENGINE=sqlite
python manage.py migrate
python manage.py seed_demo
python manage.py runserver
```

> 生产/容器内默认使用 MySQL（`mysqlclient`）。仅在显式设置
> `REVSTREAM_DB_ENGINE=sqlite` 时使用 SQLite。

---

## 2. 领域模型

```
Developer ──< App ──< Placement ──< WaterfallVersion ──< WaterfallEntry >── AdNetwork
                    │                     │  (draft/published, version_number)
                    │                     └── CurrentVersion（当前生效指针，原子切换）
                    ├──< Event（app+event_id 幂等；冻结 version/experiment/variant）
                    └──< Experiment ──< VariantVersion(A/B → 不可变版本)
                                      └─< Assignment（(实验, user哈希) 固定分组）
ScoreRun（30 分钟槽）──< ScoreItem（按广告位×网络的原始指标/归一化/总分/排名）
AuditLog（只增不改的配置审计）
```

关键约束：

- 每个广告位最多 **8** 个网络（`MAX_NETWORKS_PER_PLACEMENT`）；
- 版本内网络唯一，`priority` 与 `fallback_order` 各自必须是 `1..N` 的排列
  （优先级代表商业偏好，回退顺序代表实际请求顺序，二者允许不同）；
- 已发布版本及其条目**永不修改/删除**；新版本全部条目写齐后，在同一事务里
  翻转 `CurrentVersion` 指针（原子切换，读者不会看到半成品）；
- 事件对历史版本用 `PROTECT` 外键，曝光时服务的 `version_id` 随事件永久保留；
- 金额一律 `DecimalField(max_digits=14, decimal_places=6)`；API 金额必须以**字符串**
  提交（`"0.123456"`），传 JSON 浮点会被逐项拒绝。

---

## 3. HTTP API（均在 `/api/v1` 前缀下）

### 3.1 认证

| 接口 | 说明 |
| --- | --- |
| `POST /auth/register/` | `{username,password,email?,company_name?}` → `{token}` |
| `POST /auth/api-token/` | `{username,password}` → `{token}`（DRF Token） |

- 管理接口：`Authorization: Token <developer token>`，严格按开发者租户过滤；
- SDK/上报接口：`Authorization: ApiKey <app.api_key>`。

### 3.2 配置管理（开发者 Token）

```
CRUD  /apps/  /networks/  /placements/
GET   /placements/{id}/versions/      # 历史版本（已发布的永不消失）
GET   /placements/{id}/config/        # 当前生效版本
PUT   /placements/{id}/draft/         # 创建/整体替换草稿
POST  /placements/{id}/publish/       # 冻结草稿 -> 下一不可变版本并原子生效
CRUD  /experiments/                   # 50/50 实验（绑定两个已发布版本）
POST  /experiments/{id}/start/ | /stop/
GET   /experiments/{id}/stats/        # 每变体的填充率 / eCPM
GET   /scores/[?placement_id=]        # 最近一次评分
GET   /scores/2026-09-22T10:30Z/      # 指定时间槽
GET   /audit-logs/[?resource_type=]   # 本人配置审计
```

草稿请求体示例：

```json
{
  "note": "raise meta floor",
  "entries": [
    {"network_id": 1, "priority": 1, "fallback_order": 1, "floor_cpm": "1.800000"},
    {"network_id": 2, "priority": 2, "fallback_order": 2, "floor_cpm": "1.500000"}
  ]
}
```

### 3.3 SDK 配置下发

```
GET /sdk/config/?placement_key=home_rewarded&user_key=<稳定设备键>
Authorization: ApiKey <app.api_key>
```

- 无运行中的实验 → 返回当前生效版本（`served.assignment.type=control`）；
- 有运行实验且带 `user_key` → 稳定 50/50 分组，返回该变体绑定的**不可变版本**；
- 有实验但没带 `user_key` → 返回 control，并标记 `user_key_required=true`
  （没有稳定键不分组，避免破坏“实验期间固定分组”）；
- 响应中的 `id`（版本 ID）要求 SDK 在后续事件里原样回传（`version_id`），
  这是“保留曝光时使用的版本”的来源。

### 3.4 事件批量上报

```
POST /events/batch/
Authorization: ApiKey <app.api_key>
Content-Type: application/json
```

- 请求体为 JSON 数组，**每批最多 2000 条**（超限整批 400）；
- 幂等键：`(app, event_id)`。重复（同批重传或跨批重试）返回 `duplicate`，
  不报错、不覆盖；
- 支持乱序（收益早于曝光）与延迟上报（不校验 `occurred_at` 是否“新近”）；
- 单条校验失败不影响其他条目，响应逐条说明：

```json
{
  "received": 3, "stored": 2, "duplicate": 0, "failed": 1,
  "results": [
    {"index": 0, "event_id": "e1", "status": "stored"},
    {"index": 2, "event_id": "e3", "status": "failed",
     "errors": {"failure_reason": "required for failure events, one of ..."}}
  ]
}
```

事件字段：`event_id`、`event_type`（`impression|fill|revenue|failure`）、
`placement_id`、`occurred_at`（ISO8601）、可选 `network_id`、`version_id`、
`experiment_id`+`variant`、`amount`（revenue 必填，字符串定点数）、
`failure_reason`（failure 必填：`no_fill|timeout|error|other`）、`user_key`
  （只存 SHA-256，不存原值）。

跨租户引用（别的开发者的网络/版本、别的应用的广告位）会被逐字段拒绝。

### 3.5 端到端示例（curl）

```bash
TOKEN=$(curl -s localhost:8000/api/v1/auth/api-token/ \
  -H 'Content-Type: application/json' \
  -d '{"username":"demo","password":"demo-pass123"}' | python -c 'import sys,json;print(json.load(sys.stdin)["token"])')

curl -s "localhost:8000/api/v1/audit-logs/" -H "Authorization: Token $TOKEN"
```

---

## 4. 30 分钟评分口径

每 30 分钟运行一次（容器 `scheduler` 服务；管理命令 `run_scoring`）。

- **时间槽**：按 UTC 对齐到整 30 分钟边界（`floor_to_slot`）；
- **窗口**：半开区间 `[槽开始-7天, 槽开始)`，事件按 `occurred_at` 归属；
- **候选网络（每个广告位）**：当前生效版本中的网络 ∪ 窗口内产生过事件的网络
  （已下线但有样本的网络仍参与对比；新配置但无样本的网络以零/空样本显式出现）。

### 4.1 原始指标（定点 Decimal）

- 填充率 `fill_rate = fills / (fills + failures)`；
- 可靠性 `reliability = (机会数 − 错误数) / (fills + failures)`，
  其中 `no_fill` 属正常商业结果（只扣填充率），`timeout/error` 才算可靠性错误；
- `eCPM = revenue_usd × 1000 / impressions`。

### 4.2 缺失样本处理

- `fills + failures == 0`：填充率/可靠性原始值记 0，并且该网络**不进入**这两个
  指标的归一化参考总体（避免一个无样本的 0 把所有真实网络拉低）；
- `impressions == 0`：eCPM 同理；
- 三类样本全部缺失：总分 0，排名最后。

### 4.3 归一化与总分

- 每个广告位内部、每个指标做 min-max：`(x−min)/(max−min)`，9 位小数、half-even；
- 当 `max == min`（单网络或全部相同）：该指标所有成员归一化为 `1.0`
  （单网络瀑布拿到真实质量分，而不是 0 分）；
- 总分 = `100 × (0.40·n填充率 + 0.35·n_eCPM + 0.25·n可靠性)`，6 位小数。

### 4.4 同分排序

依次比较：总分降序 → 原始填充率降序 → 原始 eCPM 降序 → 原始可靠性降序 →
**网络 ID 升序**。末级保证重复调度结果逐字节一致，与数据库行序无关。

### 4.5 一致性、原子性与延迟补算

- 每个槽只可能有一个 `ScoreRun`（`slot_start` 唯一），构建时对该行加行锁；
  重复执行直接返回已冻结的同一条记录（数字、排名、`items_hash` 完全相同）；
- 条目全部写齐后，在同一事务中置 `finalized=true` 并更新哈希 —— 读者只能看到
  “旧槽完整”或“新槽完整”两种状态；
- 槽冻结后到达的迟到事件**不会回改**该槽，而会被之后的 7 天滚动窗口自然纳入
  （延迟补算）；可用
  `python manage.py run_scoring --backfill 2026-09-20T00:00:00Z --at ...`
  重放历史槽（同槽重放幂等）。

---

## 5. 实验分流与统计口径

- 实验创建时必须绑定两个**已发布且不可变**的版本（`version_a`/`version_b`），
  启动后不可更换 —— 这保证“配置变更不能污染历史组别”；要改配置就发布新版本、
  建新实验；
- 分组函数 `SHA-256("{实验public_key}|{user_key}")` 取模落桶，实验期内同一
  `user_key` 永远同桶；分配只依赖实验键与用户键，与配置版本无关；
- 首次见到的 `(实验, user哈希)` 落一条 `Assignment`（可观测/对账用），分组始终
  由哈希重算，服务重启结果不变；
- 曝光事件在写入时固化 `experiment/variant/version`，统计基于用户**当时实际看到
  的版本**；
- 统计口径（与评分类指标一致）：
  - 填充率 = `fills / (fills + failures)`，无样本返回 `null`（不伪造 0）；
  - eCPM = `revenue × 1000 / impressions`，无曝光返回 `null`；
  - 同时返回 impressions/fills/failures/revenue 样本量与已分配用户数。

---

## 6. 权限与审计

- 开发者只能访问自己的资源（App/Placement/网络/实验/评分/审计均按
  `Developer → App` 归属链过滤）；访问他人广告位返回 403/404，不泄漏存在性；
- 所有配置变更（增/改/删、草稿、发布、实验启停）写 `AuditLog`（开发者、动作、
  资源、变更内容、时间），只提供只读 API，不提供更新/删除入口。

---

## 7. 测试

```bash
# 无需 MySQL（SQLite 内存库）：
REVSTREAM_DB_ENGINE=sqlite python manage.py test tests -v 2

# 真实 MySQL（并发发布用例仅在 MySQL 上运行）：
docker compose up -d db
MYSQL_HOST=127.0.0.1 MYSQL_PORT=3325 python manage.py test tests -v 2
```

覆盖点（52 个用例）：

- 重复 / 乱序 / 迟到事件、批量上限、逐字段失败说明、金额定点数拒绝浮点、
  跨租户资源拒绝（`test_events.py`、`test_money_and_buckets.py`）；
- 草稿/发布、不可变版本、最多 8 网络、优先级/回退排列校验、无草稿发布
  （`test_waterfall.py`）；
- 并发发布（MySQL 行锁：恰有一个赢家、只产生一个版本号，败者 409；
  SQLite 下对应顺序化不变量断言，见 `test_concurrent_publish.py`）；
- 评分权重、归一化、缺失样本、单网络总体、同分规则、重复槽逐字节一致、
  迟到事件不回改旧槽而进入下一槽、7 天半开窗口、未冻结不可见
  （`test_scoring.py`）；
- 固定分组稳定性、约 50/50、发布新版本不挪动历史分组、版本绑定与统计口径
  （`test_experiments.py`）；
- 租户隔离与审计（`test_tenancy_audit.py`）。

---

## 8. 运维与目录

```
config/                 Django 项目（settings/路由/wsgi）
accounts/               开发者账户、注册/取 Token
applications/           App / AdNetwork / Placement 与管理 ViewSet
waterfall/              版本/条目/当前指针；发布服务（行锁+原子切换）；SDK 下发
events/                 事件模型与幂等批量摄取服务
scoring/                30 分钟槽、评分服务、管理命令、循环调度器
experiments/            实验、变体版本绑定、固定分组、分组统计
common/                 审计模型/接口、权限、定点金额/哈希工具、seed_demo
entrypoint.sh           等待 MySQL → migrate → (可选 seed) → web/scheduler
```

手动触发一次评分：

```bash
docker compose exec web python manage.py run_scoring
docker compose exec web python manage.py run_scoring --at 2026-09-22T10:30:00Z
```

重置演示数据：

```bash
docker compose down -v && docker compose up -d --build
```
