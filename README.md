# 运动命令仲裁（Motion Command Arbiter）

纯后端服务：对三个来源的速度命令进行仲裁并**只输出决策记录**（不连接、不驱动任何电机）。

- **来源**：`autonomous`（自主）、`remote`（遥控）、独立的 `estop`（急停）通道
- **急停最高优先级且锁存**：锁存期间一律零速；只有带新序号的显式 `release` 才能解除
- **每源命令携带**：单调序号 `seq`、租约 `lease_id/lease_ms`、有效期 `ttl_ms`
- **拒绝旧消息**：序号回退/重放、到达时已过 TTL 或租约、时钟回拨、越界/NaN 速度、超前时钟
- **遥控失联不复活旧自主**：遥控接管前的自主命令被标记 `stale_after_override`，
  遥控租约/TTL 到期或显式释放后必须收到**新鲜自主命令**才能恢复运动
- **查询绝不刷新租约**：GET 决策、历史、状态接口零写入；只有携带新序号的新命令才能续期
- **可控时钟**：`sim` 模式用 `X-Sim-At` 请求头驱动时间，便于确定性验证到期/解除/同刻竞争；
  另支持 `wall` 真实时钟模式
- **真实密码学**：所有写请求必须携带 HMAC-SHA256 签名（逐源密钥，恒定时间比较）
- **真实持久化**：SQLite（WAL）写穿全部状态，重启后完整水合（seq 水位、锁存、门控、历史）

技术栈：Rust + Axum 0.8 + rusqlite（bundled SQLite，无需系统库）。

---

## 1. 构建与启动

```bash
# 调试构建
cargo build

# 或发布构建（验收脚本默认使用）
cargo build --release

# 启动（sim 可控时钟，默认 127.0.0.1:8080，DB 在 data/arbiter.sqlite）
cargo run
# 或
./target/release/motion-arbiter
```

环境变量：

| 变量 | 默认值 | 说明 |
|---|---|---|
| `ARBITER_CLOCK` | `sim` | `sim`（用 `X-Sim-At` 头）或 `wall`（系统时钟，拒绝该头） |
| `ARBITER_BIND` | `127.0.0.1:8080` | 监听地址 |
| `ARBITER_DB` | `data/arbiter.sqlite` | SQLite 路径；`:memory:` 为纯临时进程 |
| `ARBITER_SIM_START` | `1000000000000` | sim 模式初始时刻（Unix 毫秒） |
| `ARBITER_KEY_AUTONOMOUS` | `dev-autonomous-secret` | 自主源 HMAC 密钥（**生产必须覆盖**） |
| `ARBITER_KEY_REMOTE` | `dev-remote-secret` | 遥控源 HMAC 密钥（**生产必须覆盖**） |
| `ARBITER_KEY_ESTOP` | `dev-estop-secret` | 急停通道 HMAC 密钥（**生产必须覆盖**） |
| `ARBITER_ADMIN_TOKEN` | 未设置 | 设置后启用 `POST /admin/reset` |
| `ARBITER_LISTEN_FD` | 未设置 | 测试用途：从该 fd 继承监听 socket |

健康检查（无需签名）：

```bash
curl -s http://127.0.0.1:8080/health
# {"clock_mode":"sim","sim_now":1000000000000,"status":"ok"}
```

---

## 2. 协议

所有写请求：

- 方法/路径见下；`Content-Type: application/json`
- sim 模式必须带 `X-Sim-At: <unix-ms>`，且不允许小于已处理时刻（时钟回拨 → 409）
- 必须带 `X-Signature: hex=<sig>`，其中
  `sig = hex(HMAC_SHA256(key, METHOD + "\n" + PATH + "\n" + X-Sim-At + "\n" + BODY))`
  （wall 模式 X-Sim-At 段为空串）
- 每个来源使用各自密钥；签名使用**原始字节**，篡改 body/路径/头即失效

### 2.1 速度命令 `POST /v1/sources/{autonomous|remote}/commands`

```json
{
  "seq": 1,
  "lease_id": "rc-lease-001",
  "vx": 0.8,
  "wz": -0.1,
  "issued_at": 1000000001000,
  "lease_ms": 3000,
  "ttl_ms": 20000
}
```

- `vx`（m/s）、`wz`（rad/s）有限幅：`|vx| ≤ 10`、`|wz| ≤ 2π`，拒绝 NaN/Inf
- 到达时 `now ≥ issued_at + ttl_ms` → 409 `ttl_expired`；`now ≥ issued_at + lease_ms` → 409 `lease_expired`
- `seq` 必须严格大于该源已接受序号，否则 409 `stale_seq`（拒绝不推进水位、可重发）
- `issued_at` 领先服务器超过 500ms → 422 `future_clock_skew`

### 2.2 急停 `POST /v1/estop`（独立密钥、独立序号空间）

```json
{ "seq": 1, "event": "latch",   "issued_at": 1000000001500, "reason": "red button" }
{ "seq": 2, "event": "release", "issued_at": 1000000003000 }
```

- `latch` 幂等（重复 latch 接受但 `effective=false`，状态保持锁存）
- 未锁存时 `release` → 409 `estop_not_latched`
- 解除会推进保鲜门控：**只有 `issued_at` 严格晚于解除时刻的命令才能重新被选中**

### 2.3 显式释放租约 `POST /v1/sources/{source}/leases/release`

```json
{ "seq": 2, "lease_id": "rc-lease-001", "issued_at": 1000000002000 }
```

- 占用源命令流的新序号（计入 seq 水位）；`lease_id` 不匹配 → 409 `lease_mismatch`
- 遥控释放后，旧自主命令同样不得复活（门控推进到释放时刻）

### 2.4 查询（只读，绝不写库/绝不刷新租约）

| 方法路径 | 含义 |
|---|---|
| `GET /v1/decision?at=<ms>` | 某时刻的仲裁投影；sim 不带 `at` 取最近 sim 时刻，wall 取系统当前时刻 |
| `POST /v1/decisions/evaluate` | 显式评估（写一条决策记录、可推进 sim 时钟，但**不续期任何租约**） |
| `GET /v1/decisions?limit=N` | 决策历史（每次写入/tick 一条；GET 不留痕） |
| `GET /v1/ingest-events?limit=N` | 被拒消息审计（stale_seq / ttl_expired / …） |
| `GET /v1/state` | 内部状态（锁存、各源水位、门控） |

### 2.5 决策响应语义

```json
{
  "decision_id": 12,
  "kind": "remote",
  "accepted": true,
  "effective": true,
  "decision": {
    "at": 2000,
    "estop_latched": false,
    "selected": { "source": "remote", "seq": 1, "lease_id": "rc-1", "vx": 0.8, "wz": -0.1 },
    "output": { "vx": 0.8, "wz": -0.1 },
    "stop_reason": null,
    "suppressed": [
      { "source": "autonomous", "seq": 1, "lease_id": "au-1", "reason": "stale_after_override" }
    ]
  }
}
```

无选中时 `selected=null`、`output={0,0}`，`stop_reason` 为 `estop_latched` 或 `no_eligible_command`。

抑制原因码：

| 原因 | 含义 |
|---|---|
| `estop_latched` | 急停锁存期间被抑制 |
| `priority_override` | 命令本身新鲜有效，但遥控在线、实时优先级更高 |
| `stale_after_override` | 自主命令不晚于最近一次遥控接管起点，遥控失联后**永不复活** |
| `stale_after_estop_release` | 命令不晚于最近一次急停解除时刻，需新鲜命令 |
| `lease_expired` / `ttl_expired` | 租约 / 保鲜期到期（开区间：`now < expires_at` 才有效） |
| `lease_released` | 租约被显式释放 |

---

## 3. 本地快速验收（一键）

```bash
cargo build --release
./scripts/acceptance.sh        # 自启服务、断言 A–I 全部场景、自动清理；退出码 0 即通过
```

覆盖：HMAC 鉴权、优先级与租约到期不复活、旧 seq/过期拒绝、急停锁存/保持/解除门控、
同刻竞争（两种到达顺序）、查询/tick 不刷新租约、显式释放、决策审计、**重启水合**。
上次运行结果：`PASS=29 FAIL=0`。

带讲解的交互演示（同样需要先启动服务）：

```bash
./target/release/motion-arbiter &      # 默认 sim 时钟，127.0.0.1:8080
./scripts/demo.sh
```

手工签名调用工具（内部用 `openssl` 真实计算 HMAC）：

```bash
scripts/call.sh remote POST /v1/sources/remote/commands 2000 \
  '{"seq":1,"lease_id":"rc-1","vx":0.8,"wz":-0.1,"issued_at":1990,"lease_ms":3010,"ttl_ms":20000}'
```

`examples/` 目录提供各类请求体的 JSON 样例（字段模板，时间戳需按场景调整）。

---

## 4. 自动化测试

```bash
cargo test                 # 全部 22 个测试
cargo test -- --nocapture  # 显示输出
```

- `src/**` 内 11 个单元测试：纯仲裁状态机 + 密码学往返/篡改
- `tests/api.rs` 11 个端到端测试：**启动真实二进制**（随机端口经 fd 继承）、
  真实 HMAC 签名、真实 SQLite 文件，包括跨进程重启验证

---

## 5. 关键设计说明

1. **两个独立时间界**：`lease_expires_at`（授权窗口）与 `ttl_expires_at`（消息保鲜窗口），
   取较早者；都是开区间 `[issued_at, expires_at)`。
2. **新鲜性双门控**：
   - `freshness_gate`：最近急停解除时刻，约束所有来源；
   - `rc_takeover_start`：最近一次遥控接管命令的 `issued_at`（或遥控租约释放时刻），
     仅约束自主源。`issued_at ≤ 门控` 的命令可被接受存储（计入审计）但永不被选中。
   - 因此"遥控在线时已到达的新鲜自主命令"表现为 `priority_override`，遥控失联后可以接手；
     而"接管之前的旧自主命令"为 `stale_after_override`，绝不复活。
3. **查询零副作用**：所有写状态路径只有命令/急停/释放/evaluate，且状态写穿在
   单个 SQLite 事务内，进程崩溃重启后从 DB 完整水合。
4. **边界确定性**：同刻（`issued_at` 相等）一律取遥控；门控是严格不等式，
   与解除/接管同刻的消息不新鲜。
5. **无电机连接**：服务输出仅是决策 JSON 与 SQLite 决策记录，不存在任何执行器 I/O。

## 目录结构

```
src/
  main.rs       配置与启动（sim/wall、fd 继承监听）
  http.rs       Axum 路由、HMAC 鉴权、请求解析
  arbiter.rs    仲裁状态机（纯投影 + 写穿事务）与单元测试
  db.rs         SQLite schema / 水合 / 决策与审计表
  crypto.rs     HMAC-SHA256 签名、恒定时间校验、密钥解码
  model.rs      数据模型、原因码
tests/          端到端集成测试（真实子进程）
scripts/        call.sh / demo.sh / acceptance.sh
examples/       请求体 JSON 样例
```
