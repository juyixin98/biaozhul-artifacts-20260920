# watchdog-host — 看门狗恢复状态机（主机端）

设备固件看门狗的**主机端恢复状态模型**，纯后端服务：Rust + Axum + SQLite。
不操作任何真实硬件——"喂狗"与"复位"都是状态迁移加 SQLite 持久化日志。

## 状态模型

- 设备有若干**关键任务**（启动参数 `--tasks`），每个任务上报**单调递增的进度计数器**。
- **重复心跳 ≠ 进度**：心跳只证明传输存活；只有计数器**增大**才算进度。
- **喂狗条件**：当前窗口内*所有*任务的计数器都推进过（`counter > baseline` 且推进时刻在窗口内），否则拒绝并列出停滞任务。
- **复位**：监督周期（`tick`）发现窗口已过期且未成功喂狗 → 记录一次复位，保存复位原因与所有任务的最后进度快照（SQLite `reset_log` 表）。
- **安全模式**：连续复位达到 `--threshold` 进入安全模式，同时 `fault_generation` 递增（新的故障代次）。
- **人工解除**：`POST /safe-mode/clear` 必须携带**当前** `fault_generation`；旧代次（或猜测的未来代次）一律拒绝（`stale_generation`）。普通心跳/喂狗**永远不能**自动解除安全模式。
- **时钟安全**：所有 elapsed 计算对时钟回退/回绕饱和为 0，绝不因时钟异常产生误复位；回退事件计入 `clock_anomalies`。
- **持久化**：`device_state`（安全模式、故障代次、连续复位数、最后复位原因）、`task_state`（最后进度计数）、`reset_log`（复位日志）全部落盘，进程重启后完整恢复。

## 构建与测试

```bash
cargo build            # 编译（Cargo.lock 已锁定依赖）
cargo test             # 15 个自动化测试：状态机 11 个 + HTTP 集成 4 个
```

测试覆盖：任务卡住、短暂抖动（窗口内恢复/边界值）、时钟回退与 u64 回绕、
重启后计数持久化、安全模式不被普通心跳解除、旧代次解除请求无效、
计数器回退拒绝、部分进度拒绝喂狗。

## 启动

```bash
# 真实时钟模式（后台监督任务自动 tick，间隔 window/4）
cargo run -- --bind 127.0.0.1:8080 --db watchdog.db \
  --tasks control-loop,sensor-fusion,logger --window-ms 5000 --threshold 3

# 可控时钟模式（时间只通过 /clock/* 推进，tick 手动触发，用于演示与验收）
cargo run -- --clock manual --db /tmp/wd-demo.db
```

## HTTP API

| 方法 | 路径 | 请求体 | 说明 |
|---|---|---|---|
| GET | `/healthz` | — | 存活探针 |
| GET | `/status` | — | 完整状态（模式、代次、复位数、各任务进度、时钟异常数） |
| POST | `/tasks/{name}/heartbeat` | `{"counter": N}` | 上报心跳+计数；`progressed` 标识是否真进度 |
| POST | `/feed` | — | 尝试喂狗；停滞→409 `stalled`，安全模式→409 `safe_mode` |
| POST | `/tick` | — | 执行一次监督周期（manual 模式下手动驱动） |
| POST | `/clock/advance` | `{"delta_ms": N}` | 推进可控时钟（仅 manual 模式） |
| POST | `/clock/set` | `{"now_ms": N}` | 设置可控时钟（仅 manual 模式，可回退） |
| POST | `/safe-mode/clear` | `{"fault_generation": N}` | 人工解除安全模式，须绑定当前故障代次 |
| GET | `/resets` | — | 复位日志（原因 + 最后进度快照 JSON） |

错误统一为 `{"error": <code>, "message": ...}`：
`bad_request`(400) / `stalled`、`safe_mode`、`stale_generation`、`counter_regression`(409) / `internal`(500)。

## 验收命令

```bash
# 1) 自动化测试（核心验收）
cargo test

# 2) 端到端演示：另开终端启动服务
cargo run -- --clock manual --db /tmp/wd-demo.db
# 然后执行演示脚本（需要 curl 和 jq）
bash examples/demo.sh

# 3) 重启持久化验证
#    在上面的服务进入安全模式后 Ctrl-C，再用同一 --db 重启：
cargo run -- --clock manual --db /tmp/wd-demo.db
curl -s http://127.0.0.1:8080/status | jq '.state | {safe_mode, fault_generation, consecutive_resets}'
#    safe_mode / fault_generation / consecutive_resets 与复位日志全部保留
```

## 设计说明

- **并发**：状态机与 SQLite 写都在一把 `Mutex` 内完成，操作短且同步，保证确定性；规模上无需连接池。
- **窗口语义**：窗口为闭区间（`elapsed <= window_ms` 内有效）；成功喂狗或复位都会开启新窗口并把各任务 baseline 锁存为当前计数。
- **复位即恢复**：复位后任务 baseline 重置为最后已知计数——设备"重启"后任务需重新证明自己在推进。
- **故障代次**：每次*进入*安全模式 `fault_generation += 1`，使此前签发的解除请求全部失效（防重放）。
- **时钟注入**：`Clock` trait（`SystemClock` / `ManualClock`），全部时间判断经 `elapsed_ms` 饱和减法，回退/回绕安全。
