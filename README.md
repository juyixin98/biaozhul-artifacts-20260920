# 运动命令仲裁后端（motion-arbiter）

纯后端服务：对三路来源的**合成速度命令**进行仲裁，只产出**决策记录**，不连接任何电机或硬件。

- **语言/框架**：Rust + Tokio + Axum 0.8
- **存储**：SQLite（`rusqlite`，bundled，无需系统安装 sqlite）
- **密码学**：每来源独立 HMAC-SHA256 密钥，请求体真实签名、常量时间校验
- **可控时钟**：`system`（真实时钟）/ `manual`（HTTP 推进，可持久化、可重启复现）

---

## 1. 仲裁语义

### 1.1 来源与优先级

| 来源 | kind | 优先级 | 说明 |
|---|---|---|---|
| 自主导航 | `autonomous` | 低 | 平时的默认速度来源 |
| 遥控 | `rc` | 高 | 租约存活期间**抢占**自主命令 |
| 急停 | `estop` | 最高 | 按下即零速并**锁存**，只能被显式 `clear` 解除 |

每类来源可配置多个具体来源 ID（如 `auto-1`、`rc-1`、`estop-1`），均带各自独立密钥与独立序号空间。

### 1.2 每条运动命令携带的字段

| 字段 | 含义 |
|---|---|
| `seq` | 来源内**严格递增**序号；`seq <= 已接受最大值` 一律拒绝（`stale_sequence`） |
| `nonce` | 一次性随机串；`(source, nonce)` 唯一，重复即拒绝（`replayed_nonce`） |
| `issue_ms` | 命令签发时刻（Unix 毫秒）。先 `GET /v1/time` 与服务器对时 |
| `valid_for_ms` | **有效期窗口**：服务器接收时刻 `now > issue_ms + valid_for_ms` 拒绝（`expired`，迟到/重放的消息无效）；`issue_ms` 超前服务器时间超过配置 `max_future_ms` 拒绝（`too_far_in_future`） |
| `lease_for_ms` | **租约时长**：命令在 `issue_ms + lease_for_ms` 之前有效；过期后不再参与仲裁。超过服务器上限 `max_lease_ms` 拒绝（`lease_too_long`） |
| `vx / vy / omega` | 合成速度（m/s、rad/s），按配置 `limits` 做逐轴限幅；急停永远输出精确 `0.0` |

注意区分两个时间概念：

- **有效期（valid_for_ms）**管的是“这条消息在网络上迟到多久还算数”——过期消息**直接拒收、不落库**；
- **租约（lease_for_ms）**管的是“命令被接受后能主导决策多久”——租约到期后命令仍在库中，但仲裁时被标记为 `lease_expired` / `rc_lease_lost`。

### 1.3 关键安全规则

1. **急停优先且锁存**：`trigger` 后仲裁恒为零速（`estop_triggered`），时间流逝、租约到期都不会解除；只有该急停来源用更大 `seq` 发出带有效签名的 `clear` 才解除。对未锁存状态发 `clear` 返回 `estop_not_active`。
2. **解除急停后旧命令全部作废**：以最近一次急停事件（trigger 或 clear）的接收时刻为**栅栏（fence）**，栅栏之前（含同刻）收到的运动命令一律标记 `invalidated_by_estop_event`，必须收到**严格晚于**该时刻的新鲜命令才能恢复运动。
3. **遥控失联后不得恢复旧自主命令**：遥控命令租约到期后，在“上一段遥控窗口结束时刻”之前接收的自主命令标记 `stale_after_remote_loss`，不会自动恢复；必须收到失联后的**新鲜自主命令**（决策原因为 `autonomous_fresh_after_rc_loss`）。
4. **查询绝不刷新租约**：`GET /v1/decision/latest`、`GET /v1/decisions/history`、`GET /admin/state`、以及 `POST /v1/evaluate {"persist": false}` 都是纯读/纯计算，不修改任何命令的租约或截止时间。只有携带新序号的新命令才能延长来源的有效时间。
5. **同刻竞争确定性**：候选命令按固定全序裁决，结果与到达顺序、线程调度无关：
   `来源优先级(rc>autonomous)` → `租约截止更晚` → `接收时刻更晚` → `seq 更大` → `来源 ID 字典序`。
   服务端另用临界区串行化“接收→校验→落库→仲裁”，并发同刻请求也不会产生竞争错乱。
6. **无可用命令即安全零速**：决策为 `source="none"`、`reason="no_active_command"`、速度全 0。

### 1.4 决策中的抑制原因码（`suppressed[].reason`）

| reason | 含义 |
|---|---|
| `higher_priority_active` | 遥控租约存活，自主命令被更高优先级抑制 |
| `superseded_by_newer_rc` | 已被同来源更新的遥控命令取代 |
| `rc_lease_lost` | 遥控命令租约到期（遥控失联） |
| `lease_expired` | 自主命令租约到期 |
| `stale_after_remote_loss` | 遥控失联后，旧自主命令不允许恢复，需新鲜命令 |
| `suppressed_while_estop_latched` | 急停锁存期间该命令被压制 |
| `invalidated_by_estop_event` | 命令接收于最近一次急停事件之前（含同刻），解除后已作废 |

决策选中原因（`chosen.reason`）：`autonomous_active` / `rc_fresh` /
`autonomous_fresh_after_rc_loss` / `estop_triggered` / `no_active_command`。
速度被限幅时 `chosen.clamped` 非空，含 `requested` 与 `emitted`。

---

## 2. HTTP 协议

所有命令接口需要两个请求头：

```
X-Source: <来源 ID，如 rc-1>
X-Signature: sha256=<hex>
Content-Type: application/json
```

签名内容 = `HMAC_SHA256(key, 域前缀 || 原始请求体字节)`，十六进制小写：

- 运动命令域前缀：`motion-command-v1\n`
- 急停命令域前缀：`estop-command-v1\n`

两个域互相独立（运动签名不能打急停接口，反之亦然）。签名覆盖**原始 body 字节**，服务端先验签再解析 JSON。参考实现见 `src/crypto.rs`（Rust）与 `scripts/send.py`（Python，标准库 `hmac`/`hashlib`）。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/healthz` | 存活探针，返回 `ok` |
| GET | `/v1/time` | 查询时钟 `{mode, now_ms}`，用于对时 |
| POST | `/v1/command` | 投递自主/遥控速度命令（202 + 即时决策） |
| POST | `/v1/estop` | 投递急停 `trigger` / `clear`（202 + 即时决策） |
| POST | `/v1/evaluate` | 在指定时刻重算决策 `{at_ms?, persist?}`；**默认落一条决策记录但不改租约**，`persist:false` 纯计算 |
| GET | `/v1/decision/latest` | 最近一条决策（纯读，不刷租约） |
| GET | `/v1/decisions/history?limit=N` | 最近 N 条决策（N 1..=500，纯读） |
| GET | `/admin/state` | 时钟、配置来源、库存计数与当前仲裁快照（纯读） |
| POST | `/admin/clock/advance` | 仅 manual 模式：`{delta_ms}` 推进时钟并持久化 |

### 运动命令请求体

```json
{
  "seq": 1,
  "nonce": "5f3a-...-随机串",
  "vx": 1.0, "vy": -0.2, "omega": 0.3,
  "issue_ms": 1700000000000,
  "valid_for_ms": 2000,
  "lease_for_ms": 1000
}
```

### 急停请求体

```json
{ "seq": 1, "nonce": "...", "action": "trigger", "issue_ms": 1700000000000, "valid_for_ms": 2000 }
```

### 响应（202）

```json
{
  "accepted": true,
  "received_ms": 1700000000050,
  "decision": {
    "at_ms": 1700000000050,
    "estop_latched": false,
    "chosen": {
      "source": "rc-1", "source_kind": "rc", "seq": 1,
      "vx": 1.0, "vy": -0.2, "omega": 0.3,
      "reason": "rc_fresh", "clamped": null,
      "deadline_ms": 1700000001000
    },
    "suppressed": [
      { "source": "auto-1", "source_kind": "autonomous", "seq": 1,
        "reason": "higher_priority_active", "detail": "遥控命令租约存活……" }
    ],
    "source_states": [ ... ]
  }
}
```

### 错误响应与状态码

`{"error":"<机器可读原因>","detail":"..."}`

| HTTP | error |
|---|---|
| 400 | `bad_json` |
| 401 | `unknown_source` / `bad_signature_header` / `bad_signature` |
| 404 | `no_decision`（尚无决策记录） |
| 409 | `conflict`（并发唯一约束竞争） |
| 422 | `bad_payload` / `stale_sequence` / `replayed_nonce` / `expired` / `too_far_in_future` / `lease_too_long` / `estop_not_active` |

每次接受与拒绝都会写入 `ingest_log` 表（含端点、来源、序号、nonce、拒绝原因、原始 body 截断），决策写入 `decisions` 表。

---

## 3. 本地启动

前置：Rust（1.80+；本机开发版本 1.98）。无其他系统依赖（SQLite 已 bundled）。

```bash
# 1) 编译（首次会拉取并编译依赖，Cargo.lock 已提交锁定版本）
cargo build --release

# 2) 生成一套带随机密钥的配置（真实 CSPRNG，输到 stdout，自行保存）
./target/release/motion-arbiter keygen > config.json

#    或直接用示例配置（密钥是公开的，仅供本地演示）
cp examples/config.example.json config.json

# 3a) 真实时钟模式启动（issue_ms 用当前墙钟毫秒）
./target/release/motion-arbiter serve \
  --config config.json --db arbiter.db --listen 127.0.0.1:8080

# 3b) 可控时钟模式启动（验收/演示推荐；时钟停在 seed，由接口推进）
./target/release/motion-arbiter serve \
  --config config.json --db arbiter.db \
  --listen 127.0.0.1:8080 --clock manual --clock-seed-ms 1700000000000
```

`config.json` 含真实密钥，已在 `.gitignore` 中，请勿提交。

### 手工发命令（自动对时、自动签名）

```bash
export ARBITER_BASE=http://127.0.0.1:8080

python3 scripts/send.py config.json auto-1 command --vx 0.5 --lease-ms 2000
python3 scripts/send.py config.json rc-1   command --vx 1.0 --lease-ms 1000
python3 scripts/send.py config.json estop-1 estop --action trigger
# 可控时钟下推进时间
curl -s -X POST $ARBITER_BASE/admin/clock/advance \
  -H 'Content-Type: application/json' -d '{"delta_ms":2000}'
python3 scripts/send.py config.json estop-1 estop --action clear
```

原始请求体示例见 `examples/payloads/`（配合 `send.py --file` 使用；注意其中的
`issue_ms` 是固定演示值，manual 模式下需与时钟对应）。

---

## 4. 验收与测试命令

```bash
# 一键端到端验收（自动构建、起停服务、真实 HMAC 签名、推进时钟、重启校验）
python3 scripts/acceptance.py

# Rust 全量测试：8 个仲裁/密码学纯逻辑单测 + 8 个真实 HTTP/SQLite 集成测试
cargo test

# 静态检查（零 warning）
cargo clippy --all-targets
```

`cargo test` 的集成测试（`tests/e2e.rs`）在随机端口启动真实 axum 服务，覆盖：

1. 遥控优先、租约到期与抑制原因；
2. 大量查询穿插后租约照常到期（查询不续命）、只读不新增决策记录；
3. `stale_sequence` / `expired` / `too_far_in_future` / `lease_too_long` 拒绝；
4. 坏签名、未知来源、跨域签名（401）；
5. 急停锁存、长时间后仍锁存、`clear` 后旧命令全失效、未锁存 clear 报错、新鲜命令恢复；
6. 同刻竞争与 8 路并发同时刻投递的确定性结果；
7. 杀进程重启后：手动时钟从 SQLite 恢复、急停状态/序号/决策历史全部持久化。

`scripts/acceptance.py` 对上述场景逐项打印 PASS/FAIL，任一失败以非零码退出。

---

## 5. 存储与模块结构

SQLite 表（启动自动建表，WAL 模式）：

- `commands`：已接受运动命令，`UNIQUE(source, nonce)` 防重放；
- `estop_events`：急停 trigger/clear 事件；
- `ingest_log`：每次投递的接受/拒绝审计记录；
- `decisions`：决策记录（列存摘要字段 + 完整 JSON）；
- `meta`：手动时钟当前值等持久化元数据。

源码：

| 文件 | 职责 |
|---|---|
| `src/arbiter.rs` | 纯函数仲裁核心（无 IO，全部规则在此，可确定性单测） |
| `src/db.rs` | SQLite 建表、序号/nonce 检查、世界视图加载、决策持久化 |
| `src/server.rs` | Axum 路由、鉴权、有效期/租约校验、临界区与错误映射 |
| `src/crypto.rs` | HMAC-SHA256 签名/验签（常量时间比较）、CSPRNG 密钥生成 |
| `src/clock.rs` | SystemClock / ManualClock |
| `src/config.rs` | 配置解析与密钥校验（≥32 字节） |
| `src/models.rs` | 请求/响应/记录的数据模型 |
| `tests/e2e.rs` | 端到端集成测试 |
| `scripts/acceptance.py` | 可交付的自动化验收脚本 |
| `scripts/send.py` | 手工签名发命令辅助工具 |

---

## 6. 设计说明与边界

- **为什么 fence 用“事件接收时刻”而不是命令 issue 时刻**：服务器只能为自己打上的接收时刻负责，不信任客户端时钟；栅栏严格按接收时刻划分，语义可审计。
- **同刻接收的命令在 clear 同刻视为作废**：`received_ms <= fence_ms` 判废。安全侧取严——要恢复运动，命令必须严格晚于解除事件到达。
- **不连接电机**：本服务只输出 JSON 决策记录；下游执行器应自行消费最新决策并对“无决策/零速”做故障安全处理。
- **生产部署注意**：当前 `/admin/*` 未加鉴权（仅限可信本地网络用于可控时钟验收）；对外部署应放到内网或加反向代理鉴权，且生产使用 `--clock system`。
