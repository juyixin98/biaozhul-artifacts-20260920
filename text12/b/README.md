# CloudGate — 多租户接入控制后端（控制平面模拟）

用 FastAPI + SQLAlchemy 2 + PostgreSQL 实现的多租户 VPN 接入**控制平面**。
只模拟接入控制（接入点、地址池、设备、租约），**不建立真实 VPN、不修改主机路由**。

## 能力总览

- **多租户隔离**：平台管理员创建租户与租户管理员；租户的接入点、地址池、设备、会话严格隔离，越权访问一律按“不存在”处理（404，不泄露资源存在性）。
- **IPv4 地址池**：建池时物化每个地址，自动排除网络地址、广播地址；可声明保留地址（如网关/VIP）。
- **原子建连**：建立会话时在数据库行锁内原子完成「接入点容量检查 → 分配唯一 IP → 绑定设备」。
  - 同一设备最多一个活动会话（PostgreSQL 部分唯一索引兜底）。
  - 重复连接请求基于 `idempotency_key` 幂等，并发重试不会重复占 IP。
  - 并发占满容量时恰好 `capacity` 个成功，其余得到 `503 capacity_exceeded`；IP 不会重复分配。
- **代次（generation）机制**：重连产生新一代租约并携带新的租约令牌；旧会话被标记 `reconnect`。心跳/关闭必须同时校验 `session_id + generation + lease_token`，旧代次迟到心跳**不能复活或延长**新会话。
- **心跳与租约**：设备每 60s 心跳，后台清扫器每 30s 扫描，10 分钟无心跳释放租约（时间均可由环境变量调整，测试中使用 1–2s）。
- **撤销**：撤销设备立即终止其活动会话并释放 IP，同时轮换设备令牌——旧令牌无法再连。撤销/重连/到期并发时，依靠只写一次的终止记录保证**同一租约只释放一次**。
- **持久化与恢复**：租约状态全部落库；服务重启后首次清扫即回收崩溃遗留的过期租约，健康租约不受影响。
- **终止审计**：`lease_terminations` 记录终止原因（`client_close / heartbeat_timeout / revoked / reconnect / capacity_admin`）与时间。

## 快速开始（Docker）

```bash
docker compose up --build web        # 启动 PostgreSQL + API（自动迁移、种子）
# API: http://localhost:8000  文档: http://localhost:8000/docs

docker compose --profile demo run --rm demo   # 端到端演示脚本
docker compose run --rm tests                 # 运行全部测试（独立 test 库，短超时）
```

默认种子账号：

| 账号 | 用途 | 用户名 / 密码 |
|---|---|---|
| 平台管理员 | 建租户、建租户管理员 | `admin` / `admin12345`（可用环境变量改） |
| 演示租户管理员 | `demo` 租户 | `demo-admin` / `demo-admin-pass` |

演示资源：租户 `demo`，池 `demo-pool`（`10.10.0.0/24`，保留 `.1/.2`），接入点 `demo-ap`（容量 250）。

## 本地运行（不用 Docker 跑应用）

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export DATABASE_URL=postgresql+psycopg2://cloudgate:cloudgate@localhost:5432/cloudgate
alembic upgrade head
uvicorn app.main:app --reload
```

## 认证

- **管理员**：`POST /api/admin/login` → Bearer JWT。
  - 平台管理员可访问任意租户；租户管理员仅限本租户。
- **设备**：注册时一次性返回 `device_token`，放在 `X-Device-Token` 头。
- **租约**：`POST /api/device/connect` 返回一次性 `lease_token`，心跳/关闭放在 `X-Lease-Token` 头。

## 关键接口

| 方法 路径 | 说明 |
|---|---|
| `POST /api/platform/tenants` | 平台管理员创建租户 |
| `POST /api/platform/tenants/{id}/admins` | 创建租户管理员 |
| `POST /api/tenants/{tid}/pools` | 建池 `{name, cidr, reserved_addresses[]}` |
| `POST /api/tenants/{tid}/access-points` | 建接入点 `{name, pool_id, capacity}`（容量不得超过池中可用地址数） |
| `POST /api/tenants/{tid}/devices` | 注册设备，返回一次性设备令牌 |
| `POST /api/tenants/{tid}/devices/{id}/revoke` | 撤销设备（立即断会话、轮换令牌） |
| `POST /api/tenants/{tid}/devices/{id}/reissue-token` | 轮换令牌（不撤销） |
| `POST /api/tenants/{tid}/devices/{id}/reinstate` | 恢复已撤销设备并发新令牌 |
| `GET  /api/tenants/{tid}/sessions` | 会话列表（可按 `device_id` 过滤） |
| `GET  /api/tenants/{tid}/sessions/{id}/termination` | 终止原因与时间 |
| `POST /api/device/connect` | 建连/重连 `{access_point_id, idempotency_key?}` |
| `POST /api/device/heartbeat` | 心跳 `{session_id, generation}` |
| `POST /api/device/close` | 主动关闭 `{session_id, generation}` |

### 建连响应示例

```json
{
  "session": {
    "id": "…", "generation": 1, "status": "active", "ip": "10.20.30.2",
    "access_point_id": "…", "device_id": "…",
    "connected_at": "…", "last_heartbeat_at": "…", "closed_at": null
  },
  "lease_token": "lt_…",
  "reconnected": false,
  "idempotent_reused": false
}
```

错误体统一为 `{"error": {"code": "...", "message": "..."}}`，常见 code：
`capacity_exceeded`(503)、`address_pool_exhausted`(507)、`stale_generation`(409)、
`invalid_lease_token`(403)、`device_revoked`(403/401)、`cross_tenant`(403)。

## 并发正确性是怎么保证的

| 风险 | 机制 |
|---|---|
| 同设备并发建连 | 先 `SELECT … FOR UPDATE` 锁设备行，串行化 connect/reconnect/revoke |
| AP 容量超卖 | 锁接入点行后在锁内计数；锁顺序固定为 设备→接入点，无死锁 |
| IP 重复分配 | 空闲地址 `FOR UPDATE SKIP LOCKED` 认领 + `(pool_id, ip) WHERE status='allocated'` 部分唯一索引 |
| 一设备多活动会话 | `(device_id) WHERE status='active'` 部分唯一索引兜底 |
| 重复释放 | `lease_terminations.session_id` 唯一约束，close/超时/撤销并发也只释放一次 |
| 旧代次迟到心跳 | 心跳/关闭校验 generation 与 lease_token，旧代次返回 409/403，不触碰新会话 |
| 撤销后旧令牌重连 | 撤销时把 `token_hash` 轮换为随机值，旧令牌认证失败 |

## 测试

```bash
docker compose run --rm tests
# 或本地（先建好 cloudgate_test 数据库，或直接用任意 *_test 库名，conftest 会自动建库迁移）
DATABASE_URL=postgresql+psycopg2://cloudgate:cloudgate@localhost:5432/cloudgate_test pytest -v
```

覆盖场景：

- `test_address_exhaustion.py`：网络/广播/保留地址排除、地址耗尽、释放后可再分配
- `test_capacity_race.py`：容量竞争（多线程真并发）、同设备并发重连唯一活动租约、并发幂等重试、并发关闭只释放一次
- `test_late_heartbeat.py`：重连代次递增、旧代次迟到心跳/关闭被拒、新旧令牌互不通用
- `test_revoke_race.py`：撤销即断即释放、旧令牌失效、撤销与心跳/到期清扫并发
- `test_cross_tenant.py`：跨租户读写 404、跨租户设备连入 403、租户管理员不能用平台接口
- `test_restart_recovery.py` / `test_restart_explicit.py`：10 分钟超时回收、重启清扫恢复、清扫幂等、后台清扫器集成
- `test_migration.py`：Alembic 在独立库上 upgrade/downgrade，校验关键部分唯一索引
- `test_api_basics.py`：登录、容量/网段校验、审计记录

## 目录结构

```
app/
  main.py        FastAPI 应用、异常映射、启动/关闭钩子
  config.py      环境变量配置
  db.py          引擎 / Session
  models.py      ORM 模型（含部分唯一索引）
  schemas.py     Pydantic 模型
  security.py    bcrypt 口令、JWT、随机令牌/哈希
  networking.py  CIDR 展开与网络/广播/保留地址判定
  leasing.py     租约生命周期与全部并发控制
  service.py     管理侧编排（池物化、容量校验等）
  bootstrap.py   种子数据
  lifecycle.py   等库、迁移、后台清扫循环
  routers/       auth / platform / tenant / device
migrations/      Alembic（启动时自动 upgrade head）
scripts/demo.py  零依赖端到端演示（标准库 urllib）
tests/           全部并发/隔离/恢复测试
```

## 配置（环境变量）

| 变量 | 默认值 | 说明 |
|---|---|---|
| `DATABASE_URL` | 本地 compose 默认 | SQLAlchemy PostgreSQL DSN |
| `JWT_SECRET` | `change-me-in-production` | JWT 签名密钥（生产必须改） |
| `JWT_EXPIRE_MINUTES` | `720` | 管理员令牌有效期 |
| `PLATFORM_ADMIN_USERNAME/PASSWORD` | `admin/admin12345` | 平台超管引导账号 |
| `HEARTBEAT_INTERVAL_SECONDS` | `60` | 约定心跳间隔（下发给客户端） |
| `HEARTBEAT_TIMEOUT_SECONDS` | `600` | 无心跳多久释放租约 |
| `SWEEP_INTERVAL_SECONDS` | `30` | 后台清扫周期 |
