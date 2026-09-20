# CloudGate 多租户接入控制后端

模拟 VPN 接入**控制平面**：多租户管理接入点、IPv4 地址池与设备；设备连接时
原子完成「容量检查 → 唯一 IP 分配 → 设备绑定」，以租约（lease）形式管理会话，
支持心跳保活、超时释放、代次（generation）校验与设备撤销。

> 本服务只模拟控制平面逻辑：**不建立真实 VPN 隧道，不创建网卡，不修改主机路由**。
> “分配 IP”仅为数据库中的租约记录。

## 技术栈

FastAPI · SQLAlchemy 2.0 · PostgreSQL 16（部分唯一索引、`SELECT … FOR UPDATE`、
SERIALIZABLE 事务、`SKIP LOCKED`）· Alembic 迁移 · pytest。

## 快速开始（Docker）

```bash
# 可选：修改超级管理员密钥
echo "CLOUDGATE_SUPER_ADMIN_KEY=$(openssl rand -hex 32)" > .env

docker compose up --build -d     # 或 make up
./scripts/demo.sh                # 端到端演示（需要 curl）
```

- API：http://localhost:18099 ，交互式文档 http://localhost:18099/docs
- PostgreSQL：`localhost:15499`（cloudgate/cloudgate）
- 容器启动时自动等待数据库并执行 `alembic upgrade head`

停止：`docker compose down`（保留数据卷）；`docker compose down -v` 同时清空数据。

## 本地开发（不使用 Docker）

```bash
pip install -r requirements.txt
createdb cloudgate_dev    # 或用任意 PostgreSQL
export CLOUDGATE_DATABASE_URL=postgresql+psycopg2://cloudgate:cloudgate@localhost:5432/cloudgate_dev
alembic upgrade head
uvicorn app.main:app --reload
```

## 测试

```bash
# 需要一个可连通的 PostgreSQL（测试库 cloudgate_test，迁移自动执行）
createdb cloudgate_test
python3 -m pytest -q          # 38 个用例，含真实多线程竞争测试
```

测试覆盖：

| 场景 | 测试 |
|---|---|
| 网络/广播/保留地址排除、地址耗尽 | `test_addressing.py`、`test_*exhaustion*`、`test_*reserved*` |
| 容量竞争（50 并发 / 容量 5） | `test_capacity_contention_never_exceeds` |
| 同设备重复/并发连接幂等 | `test_concurrent_connect_same_device_is_idempotent` |
| 重复占 IP / 关闭后地址再分配 | `test_address_pool_exhaustion_concurrent` 等 |
| 迟到心跳、错代次心跳/关闭 | `test_expiry_late_heartbeat_and_generation` |
| 撤销竞争（连接/心跳/重连同时发生） | `test_revoke_concurrent_with_*`、`test_double_revoke_*` |
| 跨租户访问 | `test_strict_tenant_isolation`、`test_connect_foreign_tenant_*` |
| 多回收器并发 + 重启恢复 | `test_concurrent_reapers_*`、`test_restart_recovery_*` |

## API 概览

认证方式：
- 超级管理员：`X-Admin-Key`（平台密钥，env `CLOUDGATE_SUPER_ADMIN_KEY`）
- 租户管理员：`X-Admin-Key`（创建租户时返回的 `admin_key`，仅展示一次）
- 设备：`X-Device-Token`（注册设备时返回的 `token`，仅展示一次）

```
POST   /admin/tenants                          超级管理员创建租户
POST   /access-points                          创建接入点（capacity）
GET    /access-points                          列出接入点（含活动会话数）
POST   /access-points/{id}/pools               增加 IPv4 地址池（cidr + 保留数）
GET    /access-points/{id}/pools
POST   /devices / GET /devices                 注册/列出设备
POST   /devices/{id}/revoke                    撤销设备（立即终止会话）
GET    /leases?status=&device_id=&access_point_id=  查询租约（严格租户过滤）
GET    /leases/{id}  /leases/{id}/events       租约详情与生命周期事件

POST   /device/sessions/connect       {access_point_id}  建立/幂等复用会话
POST   /device/leases/{id}/heartbeat  {generation}       心跳（每 60s）
POST   /device/leases/{id}/close      {generation}       主动关闭
GET    /device/sessions/current                         当前活动会话
GET    /device/me
```

## 关键设计

### 地址分配
- 池 CIDR 规范化校验；`IPv4Network.hosts()` 自动排除网络地址与广播地址，
  再按 `reserved_first/reserved_last` 扣除保留地址。
- 同一接入点的多个地址池按 first-fit 选择；地址段重叠在创建时拒绝。
- 租约表上存在 **`(access_point_id, ip_address) WHERE status='active'` 部分唯一索引**，
  数据库层面兜底，IP 绝不可能被两条活动租约同时占用。

### 会话建立的原子性与幂等
- 整个建立过程在一个 **SERIALIZABLE** 事务中，依次对设备行、接入点行、
  现存活动租约加 `FOR UPDATE` 锁，再检查容量与选址；序列化失败/唯一索引冲突自动重试。
- 设备已有活动会话时直接返回原租约（`reused=true`），所以重复请求和并发请求
  都收敛到同一条租约，不会重复占 IP。
- `(device_id) WHERE status='active'` 部分唯一索引保证“同一设备只有一个活动会话”。

### 心跳与代次
- 设备每建立一次新会话，`generation` 在历史最大值上 +1。
- 心跳/关闭都必须携带正确代次，且租约必须属于该设备且仍为 `active`：
  - 旧代次迟到心跳 → `409 stale_generation`；
  - 租约已终止后的同代次迟到心跳 → `409 lease_terminated`，**不复活、不延长**；
  - 旧代次关闭请求无法影响新会话。
- 默认 600 秒无心跳判定过期；后台每 30 秒扫描一次（可调）。

### 撤销
- 撤销在同一 SERIALIZABLE 事务内翻转设备状态并终止其活动租约（`revoked`）。
- 已撤销设备的令牌在认证层即返回 403，无法靠旧令牌重连。
- 与连接/重连/心跳/回收并发时，靠行锁 + 唯一索引 + 条件终止保持唯一租约。

### 过期回收与重启恢复
- 回收使用 `FOR UPDATE SKIP LOCKED`，多实例并发运行互不阻塞；
  只有 `active` 租约能做一次状态转换，**同一租约只释放一次**，并写审计事件。
- 所有租约状态持久化在 PostgreSQL。进程重启后的启动钩子立即执行一次回收，
  停机期间超时的租约在恢复时被清理，地址归还地址池。

### 租户隔离
- 所有管理查询都以 `tenant_id` 为强制过滤条件；跨租户引用资源统一返回 404，
  不泄露资源是否存在。设备操作通过令牌绑定的设备身份再次校验归属。
- 地址唯一性以接入点为作用域，不同租户可使用相同网段而互不影响。

## 配置（环境变量，前缀 `CLOUDGATE_`）

| 变量 | 默认值 | 说明 |
|---|---|---|
| `DATABASE_URL` | 本地 cloudgate 库 | SQLAlchemy/psycopg2 DSN |
| `SUPER_ADMIN_KEY` | dev 占位值 | 平台超级管理员密钥，生产必须覆盖 |
| `HEARTBEAT_INTERVAL_SECONDS` | 60 | 告知设备的心跳周期 |
| `HEARTBEAT_TIMEOUT_SECONDS` | 600 | 无心跳多久判定过期 |
| `REAPER_INTERVAL_SECONDS` | 30 | 后台回收扫描间隔，0 关闭 |
