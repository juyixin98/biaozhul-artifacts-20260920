# 看门狗恢复状态机（watchdog-host）

设备固件看门狗的**主机端状态模型**，纯后端服务：Rust + Axum + SQLite。
不操作任何真实硬件，所有「喂狗 / 复位 / 安全模式」都是数据库里的状态迁移。

## 1. 它建模了什么

```
            全部关键任务在窗口内推进
   ┌──────────────────────────────────────┐
   │                                      ▼
(running) ── 窗口超时 / 固件上报复位 ──► consecutive_resets += 1
   ▲                                      │
   │ 人工解除（见下）                      │ 连续复位 >= reset_threshold
   │                                      ▼
   └────────────────────────────── (safe_mode, fault_generation += 1)
            普通心跳 / 进度 / 复位上报均无法离开
```

核心规则：

1. **喂狗 = 窗口内每个已注册关键任务的进度计数都严格推进**。
   - 心跳本身不是进度：计数相同的重复心跳，无论来多少次，都**不喂狗**。
   - 只推进部分任务也不喂狗（必须「全部推进」）。
   - 成功喂狗后重新开窗，并把连续复位计数清零（短暂抖动恢复后不会被旧账拖入安全模式）。
2. **窗口超时**或**固件上报复位**：保留复位原因 / 来源 / 时间 / 最后进度，
   `consecutive_resets += 1` 并重新开窗；达到阈值 `reset_threshold` 即进入
   `safe_mode`，同时 `fault_generation += 1`（故障代次）。
3. **安全模式只能人工解除**，且解除请求必须绑定**当前故障代次**：
   - 先申请一次性随机挑战（challenge，含 TTL）；
   - 用运维密钥对 `"{generation}:{challenge}"` 计算 HMAC-SHA256；
   - 服务端做常量时间比对（`subtle`），代次不符、挑战过期/不匹配、签名错误一律 403；
   - 新故障会作废旧代次的挑战与签名，旧解除请求永久无效；挑战单次有效。
4. **安全模式下普通心跳绝不解除安全模式**（`fed=false, safe_mode=true`）；
   安全模式下固件再上报的复位会被**留痕但不计数**，防止故障计数被继续推高。
5. 进度计数是 32 位无符号整数，按自然回绕比较新旧（RFC 1982 半模数规则）：
   `0xFFFFFFFF → 0` 视为前进 1；相等视为停滞。
6. 时间判定使用朴素减法 `now - window_started >= window_ms`：
   - 时钟**回退**（NTP 回拨）不会产生伪超时；
   - 时钟跨越 32 位回绕点也不会；
   - 只有真实向前流逝的时间达到窗口长度才超时；一次判定即使跳过多个窗口也只记 1 次复位
     （设备实际只重启了一次），随后以当前时间重新开窗。
7. 所有状态落在 SQLite（WAL），**重启计数、连续复位、故障代次、复位历史跨进程持久化**。

## 2. 本地启动

前置：Rust 工具链（已在 cargo 1.98.1 验证）。SQLite 由 `rusqlite` 的 `bundled`
特性在本地真实编译，无需系统安装 libsqlite3。

```bash
cargo run --bin watchdog-host
# 可调环境变量：
#   WATCHDOG_ADDR=127.0.0.1:8080   监听地址（默认）
#   WATCHDOG_DB=watchdog.db        SQLite 文件（默认）
#   WATCHDOG_MANUAL_CLOCK=1        开启可控时钟 /admin/clock/*（仅测试/演示用）
```

健康检查：

```bash
curl -s http://127.0.0.1:8080/health
```

## 3. HTTP 协议（JSON）

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/devices` | 注册设备（窗口、阈值、挑战 TTL、运维密钥） |
| GET  | `/devices/{id}` | 完整状态（状态/代次/连续复位/最后进度/boot_count…） |
| POST | `/devices/{id}/tasks` | 注册关键任务（可给基线计数；基线**不算**推进） |
| POST | `/devices/{id}/heartbeat` | 上报各任务进度计数；返回是否喂狗 |
| POST | `/devices/{id}/reset` | 固件上报复位（reason/source/boot_count） |
| POST | `/devices/{id}/tick` | 主动求值一次窗口（超时则记复位） |
| GET  | `/devices/{id}/resets` | 复位流水（原因、来源、boot_count、计数后连续值/代次） |
| GET  | `/devices/{id}/events` | 审计事件流 |
| POST | `/devices/{id}/safe-mode/challenge` | 申请一次性解除挑战（仅 safe_mode） |
| POST | `/devices/{id}/safe-mode/clear` | 提交 `{generation, challenge, hmac_hex}` 解除 |
| GET/POST | `/admin/clock`, `/admin/clock/advance`, `/admin/clock/set` | 可控时钟（仅手动时钟模式） |

示例请求体见 [`examples/`](examples/)。

### 典型流程（手动时钟，方便观察）

```bash
export WATCHDOG_MANUAL_CLOCK=1 WATCHDOG_DB=/tmp/wd.db
cargo run --bin watchdog-host            # 终端 A
B=http://127.0.0.1:8080; D=sensor-node-07

curl -s -X POST $B/devices -H 'content-type: application/json' \
  --data @examples/create_device.json
curl -s -X POST $B/devices/$D/tasks -H 'content-type: application/json' \
  --data @examples/register_task_net.json
curl -s -X POST $B/devices/$D/tasks -H 'content-type: application/json' \
  --data @examples/register_task_storage.json

# 全部任务计数推进 -> fed=true
curl -s -X POST $B/devices/$D/heartbeat -H 'content-type: application/json' \
  --data @examples/heartbeat_full.json
# 完全相同的计数再来一次 -> fed=false（重复心跳不是进度）
curl -s -X POST $B/devices/$D/heartbeat -H 'content-type: application/json' \
  --data @examples/heartbeat_repeat.json

# 让时间越过窗口（手动时钟），连续三次 -> safe_mode
for _ in 1 2 3; do
  curl -s -X POST $B/admin/clock/advance -H 'content-type: application/json' -d '{"ms":1000}'
  curl -s -X POST $B/devices/$D/tick
done
curl -s $B/devices/$D | jq '{status,fault_generation,consecutive_resets,last_reset_reason}'

# 安全模式下普通心跳无效
curl -s -X POST $B/devices/$D/heartbeat -H 'content-type: application/json' \
  -d '{"reports":[{"task_id":"net-loop","counter":999},{"task_id":"storage-flush","counter":999}]}'

# 人工解除：申请挑战 -> 用密钥签名 -> 提交
NONCE=$(curl -s -X POST $B/devices/$D/safe-mode/challenge | jq -r .challenge)
SIG=$(cargo run --quiet --bin sign-clearance -- \
        --key dev-operator-key-change-me --generation 1 --challenge "$NONCE")
curl -s -X POST $B/devices/$D/safe-mode/clear -H 'content-type: application/json' \
  -d "{\"generation\":1,\"challenge\":\"$NONCE\",\"hmac_hex\":\"$SIG\"}"
```

签名工具 `sign-clearance` 与服务端执行同一份真实 HMAC-SHA256 计算；
验收脚本还会用 python3 的 hmac 独立算一遍并与 Rust 结果交叉比对（必须一致）。
生产上应在运维侧离线保管密钥并签名，密钥本身从不随请求发送。

## 4. 自动化测试与验收

```bash
# 单元 + 13 个端到端集成测试（内存/临时文件 SQLite、确定性时钟）
cargo test

# 真实 HTTP 端到端验收：启动服务器，用 curl/jq 打全流程，
# 含「主机进程重启后计数持久化」「python3 与 Rust HMAC 交叉校验」
bash scripts/acceptance.sh
```

测试覆盖的关键性质：

- 喂狗要求**所有**任务推进；重复心跳不喂狗；
- 任务卡住 → 连续复位到阈值 → 安全模式，且**普通心跳无法解除**；
- 短暂抖动：一次成功喂狗把连续复位清零；
- 时钟**回退**与 32 位**回绕**均不产生伪超时；跨越多个窗口只记一次复位；
- 进度计数 32 位回绕（`0xFFFFFFFF → 0`）算推进；
- 旧代次解除请求被拒（403 stale），篡改 HMAC 被拒，挑战过期需重新申请，
  正确签名解除后 nonce 单次有效；新故障作废旧签名；
- 安全模式下上报的复位**留痕但不计数**；
- **进程重启**后 status / fault_generation / consecutive_resets / boot_count /
  复位历史全部保留，且安全模式在重启后仍不能被心跳解除。

## 5. 目录

```
Cargo.toml / Cargo.lock        # 依赖与锁定版本
src/clock.rs                   # Clock trait：SystemClock + 可控 ManualClock
src/db.rs                      # SQLite schema（WAL）
src/model.rs                   # 请求/响应与常量
src/service.rs                 # 状态机核心（喂狗/超时/复位/安全模式/HMAC 校验）
src/http.rs                    # Axum 路由
src/main.rs                    # 服务入口
src/bin/sign_clearance.rs      # 离线 HMAC 签名小工具
tests/state_machine.rs         # 集成测试
scripts/acceptance.sh          # 端到端验收脚本
examples/                      # 示例 JSON 输入
```

## 6. 说明与边界

- 单机单进程、互斥锁串行化状态迁移；鉴权/多租户不在范围内。
- 心跳在安全模式下仍被接受（更新 last_report_ms），只是明确不喂狗、不解除。
- 32 位进度计数若发生超过半模数（约 21 亿）的单次跳变，按序列号语义视为回退；
  正常逐 tick 推进与回绕不受影响。
- 不做真实硬件动作；`boot_count` 为固件上报、主机持久化的单调最大值。
