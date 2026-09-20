# CloudGate — 多租户接入控制后端（控制平面模拟）

CloudGate 是一个多租户网络接入控制平面：租户管理接入点（Access Point）、IPv4 地址池和设备；
设备通过接入点建立会话（session/lease），获得池内唯一 IPv4 地址，并通过心跳维持租约。
**仅模拟控制平面**：不建立真实 VPN 隧道，不修改主机路由或网络配置。

技术栈：FastAPI · SQLAlchemy 2.0 · PostgreSQL · Alembic · Docker。

## 快速开始

### Docker（推荐）

```bash
docker compose up --build
# API: http://127.0.0.1:8000  （交互文档 /docs）
# 默认全局管理员: admin / admin123 （可用 CLOUDGATE_ADMIN_* 覆盖）
```

容器启动时自动执行 `alembic upgrade head` 并引导初始管理员。

### 本地开发

```bash
pip install -r requirements.txt
createdb cloudgate
export CLOUDGATE_DATABASE_URL=postgresql+psycopg2://cloudgate:cloudgate@localhost:5432/cloudgate
alembic upgrade head
uvicorn app.main:app --reload
```

### 演示与测试

```bash
python3 scripts/demo.py http://127.0.0.1:8000   # 端到端演示（需服务已启动）

createdb cloudgate_test
export CLOUDGATE_TEST_DATABASE_URL=postgresql+psycopg2:///cloudgate_test
python3 -m pytest                                # 28 个测试
```

## 模型与配置

- **Tenant**：租户。所有资源（地址池、接入点、设备、租约）均挂在租户下，严格隔离。
- **IPv4Pool**：CIDR + 保留地址列表（JSONB）。分配时排除网络地址、广播地址与保留地址。
- **AccessPoint**：`capacity` 限制并发会话数，绑定一个地址池。
- **Device**：持有一次性下发的 Bearer token（仅存 SHA-256 哈希）；`generation` 为单调递增的会话代次。
- **Lease**：会话/租约。`state: active|released`，`expires_at = last_heartbeat + TTL`，
  终止时记录 `released_at` 与 `release_reason`（`disconnected|expired|revoked`），记录永久保留。

关键配置（环境变量前缀 `CLOUDGATE_`）：

| 变量 | 默认 | 说明 |
|---|---|---|
| `DATABASE_URL` | 本地 postgres | SQLAlchemy 连接串 |
| `JWT_SECRET` | dev-secret | 管理员 JWT 签名密钥 |
| `HEARTBEAT_INTERVAL_SECONDS` | 60 | 设备心跳间隔（connect 响应中下发） |
| `LEASE_TTL_SECONDS` | 600 | 10 分钟无心跳租约过期 |
| `SWEEP_INTERVAL_SECONDS` | 30 | 后台过期清扫周期；启动时总是先清扫一次（重启恢复） |
| `ADMIN_USERNAME` / `ADMIN_PASSWORD` | admin / admin123 | 初始全局管理员 |

## API 概览

管理员（`Authorization: Bearer <JWT>`，`POST /admin/login` 获取）：

```
POST /admin/tenants                              创建租户（仅全局管理员）
GET  /admin/tenants
POST /admin/tenants/{tid}/admins                 创建租户级管理员
POST /admin/tenants/{tid}/pools                  创建地址池（cidr + reserved）
POST /admin/tenants/{tid}/access-points          创建接入点（pool_id + capacity）
POST /admin/tenants/{tid}/devices                注册设备（token 仅本次返回）
POST /admin/tenants/{tid}/devices/{id}/revoke    撤销设备（立即终止其会话）
GET  /admin/tenants/{tid}/leases[?state=...]     租约列表
GET  /admin/tenants/{tid}/terminations           终止记录（原因 + 时间）
```

设备（`Authorization: Bearer <设备token>`）：

```
POST /device/connect      {access_point_id, idempotency_key}
POST /device/heartbeat    {lease_id, generation}
POST /device/disconnect   {lease_id, generation}
GET  /device/session      当前活动会话
```

错误语义：`401` 认证失败/设备已撤销 · `404` 资源不存在或跨租户不可见 ·
`409` 容量已满/地址池耗尽/代次过期/已有活动会话 · `410` 会话已终止 · `422` 参数非法。

## 并发与一致性设计

- **原子建会话**：`connect` 在单事务内先锁设备行（`SELECT ... FOR UPDATE`，与撤销互斥），
  再锁接入点行做容量检查，最后锁地址池行挑选空闲 IP。并发连接不会超容量、不会重复占地址。
- **数据库兜底唯一性**（应用逻辑失效时也不会错）：
  - 部分唯一索引 `(pool_id, ip) WHERE state='active'` —— 同一地址不会同时租给两个会话；
  - 部分唯一索引 `(device_id) WHERE state='active'` —— 同一设备至多一个活动会话；
  - 唯一约束 `(device_id, idempotency_key)` —— 重复/并发重试的连接请求幂等返回同一租约。
- **代次（generation）**：每次新会话使设备代次 +1 并写入租约；心跳与断开必须携带当前代次，
  旧会话的迟到心跳返回 `409/410`，既不能复活旧租约，也不能延长新租约。
- **只释放一次**：所有释放路径（断开、过期清扫、撤销）都是条件更新
  `UPDATE ... WHERE state='active'`，并发释放只有一个赢家，终止记录只写一次。
- **过期清理**：后台 sweeper 周期执行单条原子 `UPDATE`；心跳触碰过期租约时也会惰性释放，
  正确性不依赖 sweeper 是否及时运行。服务重启后启动流程立即清扫一次（租约持久化，可恢复）。
- **撤销**：撤销与终止会话在同一事务（锁设备行 → 置 `revoked` → 条件释放活动租约），
  旧 token 立即失效（认证层检查 `revoked`），无法重连。
- **租户隔离**：租户级管理员 JWT 携带 `tid` 声明，越权访问返回 404（不泄露存在性）；
  设备 token 天然绑定租户，跨租户接入点/租约均不可见。

## 测试覆盖（tests/，28 个用例）

- `test_allocation.py` —— 网络/广播/保留地址排除、地址池耗尽、释放后地址复用
- `test_concurrency.py` —— 容量竞争（容量 1，8 并发仅 1 成功）、地址池竞争（6 地址 10 并发，
  IP 不重复）、同设备同幂等键并发（全部返回同一租约）
- `test_sessions.py` —— 连接/心跳/断开、幂等重放、单活动会话、代次校验、重连换代次
- `test_expiry_recovery.py` —— 心跳续期、惰性过期、清扫释放、迟到心跳不能复活/延长、
  重启恢复清扫且同一租约只释放一次
- `test_revocation.py` —— 撤销即终止、旧 token 失效、撤销×心跳×断开竞争、撤销×过期清扫竞争
- `test_admin_isolation.py` —— 跨租户管理员/设备隔离、认证失败、参数校验

## 目录结构

```
app/
  config.py            配置（CLOUDGATE_* 环境变量）
  models.py            SQLAlchemy 模型（含部分唯一索引）
  security.py          口令哈希 / 设备 token / JWT
  deps.py              认证与租户作用域依赖
  services/leases.py   租约生命周期（全部并发控制在此）
  routers/admin.py     管理端 API
  routers/device.py    设备端 API
  main.py              应用工厂、启动恢复、后台清扫
alembic/               迁移（0001_initial）
tests/                 pytest 测试（真实 PostgreSQL）
scripts/demo.py        端到端演示
Dockerfile / docker-compose.yml
```
