# Sentinel

员工活动异常检测服务。本地批量接收终端事件、按规则定位内部威胁并支持警报分诊。

* **技术栈**：Python 3.12 · FastAPI · SQLAlchemy 2 · PostgreSQL 16 · Alembic · JWT · bcrypt
* **唯一外部依赖是 PostgreSQL**；没有桌面采集代理，没有前端。
* 交互式 API 文档：服务启动后访问 `/docs`（Swagger）或 `/redoc`。

## 1. 快速启动（Docker）

```bash
cd sentinel
docker compose up --build
```

启动内容：

| 服务 | 地址 | 说明 |
| --- | --- | --- |
| API | http://localhost:8000 | 自动执行迁移并播种演示数据（`SENTINEL_SEED_DEMO=1`） |
| PostgreSQL | localhost:5432 | 库/用户/密码均为 `sentinel` / `sentinel` / `sentinel`，库名 `sentinel_b` |

健康检查：`GET /health`。停止并清空数据：`docker compose down -v`。

## 2. 本地（非 Docker）运行

需要一个可连接的 PostgreSQL：

```bash
createdb sentinel_b   # 或在已有实例上建库
python -m venv .venv && source .venv/bin/pip install -r requirements-dev.txt
export SENTINEL_DATABASE_URL='postgresql+psycopg://sentinel:sentinel@localhost:5432/sentinel_b'
alembic upgrade head
python -m scripts.seed_demo          # 可选：演示数据
uvicorn app.main:app --reload
```

配置项全部以 `SENTINEL_` 为前缀（见 `.env.example`）：`DATABASE_URL`、`JWT_SECRET`、
`JWT_EXPIRE_MINUTES`，以及检测参数 `WORKDAY_START_HOUR=6`、`WORKDAY_END_HOUR=22`、
`DOWNLOAD_WINDOW_MINUTES=10`、`DOWNLOAD_THRESHOLD=50`、`BASELINE_DAYS=14`、`BASELINE_ZSCORE=3.0`。

## 3. 演示账号

播种脚本创建以下账号，**密码统一为 `Passw0rd!`**：

| 用户名 | 角色 | 可见范围 |
| --- | --- | --- |
| `admin` | admin | 全部 |
| `lee` | analyst | Engineering 部门子树 |
| `raul` / `maya` | manager | 各自的直接下属 |
| `sam` `evan` `neo` `zoe` `yuki` | employee | 被监控对象（无 API 权限） |

演示数据覆盖：非工作时段访问、10 分钟 51 次下载、首次 USB、14 天基线异常（sam）、
零方差（zoe）、历史不足（neo）。

## 4. API 说明

除 `/health`、`/auth/login`、`/auth/token` 外所有接口都需要
`Authorization: Bearer <token>`。获取令牌：

```bash
curl -X POST localhost:8000/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"username":"admin","password":"Passw0rd!"}'
```

也支持 OAuth2 表单登录 `POST /auth/token`（Swagger UI 的 Authorize 使用它）。

### 4.1 批量摄取事件

`POST /events/batch`（角色：admin、analyst）

```json
{
  "events": [
    {
      "event_id": "evt-001",
      "device_key": "dev-sam",
      "type": "file_download",
      "occurred_at": "2026-09-19T14:02:00-04:00",
      "payload": {"file": "/downloads/bundle_3.zip"}
    }
  ]
}
```

* 每批 **1–2000 条**；`occurred_at` 必须带时区偏移，否则 422。
* 去重键为 `(device_key, event_id)`。
  * 同键同内容重传 → 计为 `duplicates`，不重复入库、不重复报警。
  * 同键**不同内容** → `409 event_content_conflict`，整批回滚。
  * 批内出现重复键、未知设备、非法字段 → `400/422`，整批回滚。
  * 并发相同重传由数据库唯一约束保证只有一份落库（返回的 `inserted` 由
    `INSERT ... ON CONFLICT ... RETURNING (xmax=0)` 精确统计）。
* 响应：`{received, inserted, duplicates, alerts_created, alert_ids}`。
* 检测与摄取在**同一事务**内完成。

事件类型：`file_access`、`file_download`、`usb_connect`、`login`、`website_visit`。

### 4.2 检测规则

1. **非工作时段访问 `off_hours_access`**：事件本地时间（员工所属组织时区）落在
   `[06:00, 22:00)` 之外，即 06:00 含、22:00 不含。去重键为设备+事件，迟到事件不会重复报警。
2. **下载突增 `download_burst`**：半开滑动窗口 `(t-10min, t]`（含当前时刻、
   不含整 10 分钟边界）内 `file_download` **超过 50**（即 ≥51）。每次摄取对受影响
   区域全量重算，结果与到达顺序无关；迟到事件若揭示出新的窗口起点且与既有突发
   相连，会**合并**进既有警报窗口而不是产生第二条警报；相隔独立的突发仍各自报警。
3. **首次 USB `usb_first`**：每位员工仅一条；迟到的更早 USB 事件会把既有警报
   的证据重定向到真正最早的事件，仍然只有一条。
4. **文件访问基线 `file_access_baseline`**：见 4.4。

### 4.3 警报查询与分诊

* `GET /alerts`（admin/analyst/manager）查询参数：`status`、`user_id`、`rule`、
  `limit`（≤200）、`offset`。
  * manager 只能看到**直接下属**；analyst 只能看到**授权部门及其子部门**成员；
    越权访问返回 **404**（不泄露资源是否存在）。
* `GET /alerts/{id}`
* `POST /alerts/{id}/triage`（在自己可见范围内）：

```json
{"status": "confirmed", "version": 1, "note": "已联系本人核实"}
```

  * `status` 可选 `confirmed` / `false_positive` / `investigated`；不能回到 `open`。
  * `version` 必须是客户端看到的当前版本；并发分诊时后写者得到
    **409 version_conflict**（响应中带服务端当前版本），不会静默覆盖。
  * 每次分诊写入一条不可变的 `alert_investigations` 审计记录（含证据快照、
    操作人、版本变化）。基线重算不会修改这些记录。

### 4.4 基线

* `POST /baselines/recompute`（admin、analyst，受部门范围限制）：

```json
{"user_ids": [5, 8], "target_day": "2026-09-19"}
```

  `target_day` 省略时默认"员工本地时区的昨天"。使用该日之前 **14 个完整本地日**
  （含零活动日）的 `file_access + file_download` 计数建立均值/标准差，对
  `target_day` 当日计数计算单侧 z-score，`z >= 3.0` 产生高优先级警报。
  * 历史不能覆盖完整 14 天 → `status=insufficient_history`（`days_used` 给出实际跨度）。
  * 方差为 0 → `status=zero_variance`，不产生 z-score、不报警。
  * 每次重算**追加新版本行**（`version` 递增），旧版本、既有警报和调查记录
    **永不覆盖**；同一 `target_day` 的警报按 `(用户,日期)` 去重。
* `GET /baselines/users/{id}/latest`、`GET /baselines/users/{id}/versions`：
  读取最新版本或全部历史版本（同样受可见范围限制）。

## 5. 数据模型要点

* `organizations.path` 为物化祖先路径（如 `/1/2/4/`），前缀匹配即子树，
  分析师授权用部门子树判定。
* `events` 上有唯一约束 `(device_id, event_id)`，并保存规范化 JSON 的
  SHA-256 `content_hash` 用于冲突判定；另有 `(user_id,type,occurred_at)` 检测索引。
* `alerts.dedup_key` 全局唯一，是所有规则幂等性的基础；`version` 为乐观锁。

## 6. 测试

```bash
# 需要一个可登录的 PostgreSQL（测试库不存在会报错，请先建库）
sudo -u postgres psql -c "CREATE ROLE sentinel LOGIN PASSWORD 'sentinel';" 2>/dev/null
sudo -u postgres createdb -O sentinel sentinel_b_test
python -m pytest
```

34 个用例覆盖：乱序/重复/冲突摄取、批校验与回滚、并发重传、工作时段时区边界、
10 分钟半开窗口、下载突增合并、USB 首次、基线三种状态与版本保留、并发分诊
（真实多会话行锁竞争）、经理/分析师/员工越权。

## 7. 目录

```
sentinel/
├── app/
│   ├── main.py            # FastAPI 装配
│   ├── config.py          # 环境变量配置
│   ├── database.py models.py schemas.py security.py errors.py
│   ├── core/access.py     # 经理/分析师可见范围
│   ├── core/triage.py     # 乐观锁分诊
│   ├── detection/
│   │   ├── engine.py      # 摄取 + 三条流式规则
│   │   ├── baseline.py    # 14 天 z-score 基线
│   │   ├── timeutil.py hashes.py
│   └── routers/           # auth / events / alerts / baselines
├── migrations/            # Alembic
├── scripts/seed_demo.py
├── tests/
├── Dockerfile docker-compose.yml docker-entrypoint.sh
└── requirements*.txt
```
