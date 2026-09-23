# quota-reserve — 多租户文件块配额预留服务（Rust + Axum）

纯后端 HTTP 服务。上传文件块前先**预留（reserve）**额度，上传完成后**提交（commit）**
转为实际占用，失败可**取消（cancel）**释放；预留超过 TTL 由**注入时钟**判定超时并释放。
配额在**字节数**和**对象（块）数**两个维度独立限制。全部状态转换持久化到 SQLite。

## 模型与不变量

```
租户配额: byte_limit, object_limit
账本:     reserved（活跃预留） + committed（实占）
不变量:   reserved.bytes  + committed.bytes  <= byte_limit
          reserved.objects + committed.objects <= object_limit
```

预留生命周期：

```
 reserved ──commit──▶ committed   （实占，不可取消）
    │
    ├──cancel──▶ cancelled         （释放；重复取消幂等，不多释放）
    └──now >= expires_at──▶ expired（释放；由注入时钟判定）
```

要点：

- **并发不超订**：每次预留都在 `BEGIN IMMEDIATE` 事务内“先扫到期 → 再聚合已占用 →
  两维检查 → 插入”，SQLite 写锁把并发预留串行化，超额请求得到 `409 quota_exceeded`。
- **超时**：到期判断只依赖注入的 `Clock`（生产为系统时钟，测试为可拨弄的假时钟）。
  `reserve` / `usage` / `commit` / `cancel` 都会先把到期的 reserved 行落为 `expired`，
  也可主动 `POST /admin/sweep`。
- **重复取消不多释放**：取消是一条带 `WHERE status='reserved'` 条件的更新，
  对已取消预留返回 `200 cancelled`（幂等）但不再改动账本。
- **持久化**：SQLite（WAL + `synchronous=FULL`），所有状态转换在事务内提交；
  进程重启后账本一致，超时按当前时钟继续判定。

## 依赖

- Rust（stable，2021 edition；开发时使用 1.8x 稳定版即可）
- 网络可访问 crates.io（构建期拉取 axum / tokio / rusqlite(bundled) / serde / uuid）
- `rusqlite` 使用 **bundled** 特性，编译时自带 SQLite，**无需系统安装 libsqlite3**；
  仅需 C 编译器（Linux 上的 gcc/cc）。

## 启动

```bash
cargo run --release
# 可选环境变量：
#   QUOTA_DB=quota.db        数据库文件路径
#   QUOTA_ADDR=127.0.0.1:8080 监听地址
```

健康检查：

```bash
curl -s http://127.0.0.1:8080/health
```

## 测试

```bash
cargo test
```

测试覆盖：两维额度检查、提交转实占、取消释放、**重复取消不多释放**、
注入时钟下的超时释放与提交拒绝、非法状态转换、**20 线程并发预留不超订**、
提交/取消/超时穿插的完整验收场景、以及数据库重开后的持久化一致性；
另有两条走真实 Axum 路由的 HTTP 集成测试。

实际构建、测试与真实 HTTP 运行结果见 [`RUN_RESULTS.md`](RUN_RESULTS.md)。

## HTTP 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| `PUT`  | `/tenants/{tenant_id}` | 创建/注册租户及配额 |
| `GET`  | `/tenants/{tenant_id}/usage` | 查询预留/实占用量（先扫到期） |
| `POST` | `/tenants/{tenant_id}/reservations` | 预留额度 |
| `GET`  | `/reservations/{id}` | 查询预留详情与状态 |
| `POST` | `/reservations/{id}/commit` | 提交：预留 → 实占 |
| `POST` | `/reservations/{id}/cancel` | 取消：预留 → 释放（幂等） |
| `POST` | `/admin/sweep` | 按当前时钟主动扫描并释放到期预留 |

时间戳均为**毫秒**（Unix epoch）。

### 1) 创建租户

```bash
curl -s -X PUT http://127.0.0.1:8080/tenants/acme \
  -H 'content-type: application/json' \
  -d '{"byte_quota": 1000, "object_quota": 10}'
# 201
```

### 2) 预留

```bash
curl -s -X POST http://127.0.0.1:8080/tenants/acme/reservations \
  -H 'content-type: application/json' \
  -d '{"byte_size": 600, "object_count": 6, "ttl_ms": 60000}'
# 201
# {"reservation_id":"……","expires_at":1700000060000}
```

超额时：

```json
HTTP 409
{
  "error": "quota_exceeded",
  "detail": {
    "bytes":   {"used": 600, "requested": 500, "limit": 1000},
    "objects": {"used": 6,   "requested": 5,   "limit": 10}
  }
}
```

### 3) 提交（转实占）

```bash
curl -s -X POST http://127.0.0.1:8080/reservations/<id>/commit
# 200 {"reservation_id":"……","status":"committed"}
```

到期后提交 → `410 {"error":"reservation_expired"}`；
已取消再提交 → `409 {"error":"illegal_transition"}`。

### 4) 取消（释放，幂等）

```bash
curl -s -X POST http://127.0.0.1:8080/reservations/<id>/cancel
# 200 {"reservation_id":"……","status":"cancelled"}   ← 再调一次结果相同，不会多释放
```

### 5) 查询用量

```bash
curl -s http://127.0.0.1:8080/tenants/acme/usage
```

```json
{
  "tenant_id": "acme",
  "byte_limit": 1000,
  "object_limit": 10,
  "reserved_bytes": 0,
  "reserved_objects": 0,
  "committed_bytes": 600,
  "committed_objects": 6,
  "total_bytes": 600,
  "total_objects": 6,
  "at_ms": 1700000060123
}
```

### 6) 预留详情 / 超时扫描

```bash
curl -s http://127.0.0.1:8080/reservations/<id>
curl -s -X POST http://127.0.0.1:8080/admin/sweep   # {"expired_count": 0}
```

一份可直接运行的端到端示例脚本见 [`examples/curl-demo.sh`](examples/curl-demo.sh)：

```bash
bash examples/curl-demo.sh
```

## 错误码约定

| HTTP | error | 场景 |
|---|---|---|
| 404 | `tenant_not_found` / `not_found` | 租户或预留不存在 |
| 409 | `quota_exceeded` | 预留+实占将越过字节或对象上限 |
| 409 | `tenant_exists` / `illegal_transition` | 重复建租户 / 非法状态转换 |
| 410 | `reservation_expired` | 预留已超时 |

## 未完成 / 取舍说明

- 单机 SQLite + 单连接互斥：足以保证强一致与串行化，未做分库/高可用；
  如需水平扩展应换中央存储（如 Postgres `SELECT … FOR UPDATE` 或专门的配额服务）。
- 没有鉴权 / 租户隔离的网络层访问控制（演示服务，默认只监听 127.0.0.1）。
- 后台没有定时清理线程；超时在请求路径上惰性判定，`/admin/sweep` 供主动扫描，
  生产可挂一个按固定周期调用该接口的定时器。
- 时间用注入的毫秒时钟，避免真实 sleep，测试可确定性复现超时。
