# 检查点与输出事务（Checkpoint & Output Transaction）纯后端原型

一个零外部依赖的 Java 事件流计算库 + JSON 服务，演示并验证：

- **输入偏移（input offset）**、**算子状态（operator state）**、**本地输出日志（local output log）**的检查点协议；
- 恢复后对**受控本地汇总表**实现**无重复提交（exactly-once 语义）**；
- 在**状态写入**与**输出提交**的每个阶段注入故障（异常 / 真正 `Runtime.halt`），证明恢复执行的最终偏移与汇总与无故障连续执行**完全一致**；
- 明确**适用边界**：该保证**不能**推广到任意外部副作用。

无消息系统、无数据库、无前端。仅需 **JDK 17+**（在 JDK 21 上验证），用 `javac` 直接构建。

---

## 1. 目录布局

```
src/main/java/dev/example/cp/
  json/      迷你 JSON 解析/生成（零依赖）
  core/      Event、Operator 接口、KeyedSumOperator、OperatorSnapshot
  engine/    Engine（事务编排+恢复）、Clock、CheckpointScheduler、状态载体
  storage/   SourceLog、StateStore、OutputStore、SummaryTable、DurableFiles
  fail/      CrashPoint、FailureInjector、InjectedCrash
  cli/       命令行入口
  svc/       HTTP JSON 服务（JDK 内置 httpserver）
src/test/java/  自研迷你测试框架 + 10 个测试类
examples/       样例输入与请求
scripts/        build.sh / test.sh / demo-faults.sh
```

数据目录（`--data DIR`）：

```
DIR/
  source/events.log                 # 仅追加输入日志（JSONL），行号即偏移
  state/latest.json                 # 已发布的最新检查点（算子状态 + lastConsumedOffset）
  state/latest.tmp                  # 未发布的临时文件（崩溃残留，恢复时清理）
  output/committing/chk-<id>.pending    # 已写出但未提交的 epoch 输出
  output/committing/chk-<id>.committed  # 已提交的 epoch 输出（原子 rename 而来）
  output/table.json                 # 受控本地汇总表（含 appliedEpoch + lastConsumedOffset）
  faults/injected.json              # 一次性故障注入规则（跨进程持久化）
```

---

## 2. 模型与术语

- **事件**：`Event(offset, key, value)`。偏移即事件在输入分区中的行号（从 0 起）。
- **算子**：`KeyedSumOperator` 维护完整的 `key -> 累计和` 内存表，确定性、单线程。
  每处理一条事件输出该 key 更新后的累计和。这是“小数据精确参考实现”：快照是**完整状态**，
  无近似结构、无增量日志压缩。
- **epoch（检查点）**：一次屏障把一段输入连同产生的输出、状态原子地推进。
  epoch id 从 1 起单调递增。
- **可注入时间/调度**：`Clock`（墙钟可替换为虚拟时钟）与 `CheckpointScheduler`
  （手动 / 每 N 条 `CountScheduler` / 时间间隔 `IntervalScheduler`）。

### 检查点事务（每个 epoch 的固定顺序）

```
1. writePending : 输出写入 output/committing/chk-<id>.pending   (fsync + 原子发布)
2. STATE_WRITE  : 状态写入 state/latest.tmp → 原子 rename 为 latest.json
   〔危险区：演示用的“外部副作用”钩子在此触发——见第 6 节〕
3. COMMIT_RENAME: pending → committed   （单原子 rename = 输出事务提交点）
4. TABLE_APPLY  : 用该 epoch 的完整快照原子重写 output/table.json，并推进 appliedEpoch
5. AFTER_COMMIT : 全部持久化完成
```

每个 rename 都是提交点：崩溃后世界只能看到“旧版本”或“新版本”，看不到半个文件。

### 恢复协议（`Engine.open` 自动执行）

1. 清理未发布残片（`latest.tmp`、`*.write-tmp`）。
2. 读 `state/latest.json`：有则恢复算子状态与 `lastConsumedOffset`；无则空状态、偏移 -1。
3. **committed 但表落后**（崩在 4/5）：按 epoch 升序，用 `appliedEpoch` 去重，幂等补应用到表。
4. **pending 文件**：
   - `id == stateEpoch`：状态已发布而输出未提交（崩在 3）→ 补提交 + 补应用；
   - `id > stateEpoch`：状态从未发布（崩在 1/2）→ 整个 epoch **作废**，其事件恢复后**重放**。
5. 收敛断言：`appliedEpoch == stateEpoch`，且表与状态的偏移一致，否则拒绝启动。

关键不变量：**汇总表永远停在某个已完整提交的 epoch 前缀**，不会包含半个 epoch。

---

## 3. 快速开始

```bash
./scripts/build.sh            # 仅主程序（产物 build/cp-tx）
./scripts/test.sh             # 构建并运行全部自动化测试
./scripts/demo-faults.sh      # 五个故障点的“崩溃+恢复”端到端演示（异常模式）
./scripts/demo-faults.sh --halt   # 用 Runtime.halt(99) 真正杀进程（接近 kill -9）
```

手动 CLI：

```bash
CP=build/cp-tx
$CP init   --data /tmp/demo
$CP load   --data /tmp/demo --file examples/events.json
$CP run    --data /tmp/demo --every 5      # 每 5 条一个 epoch
$CP status --data /tmp/demo                # 打开即恢复，打印偏移/汇总
$CP peek   --data /tmp/demo                # 只读磁盘现场，不触发恢复
$CP fault  --data /tmp/demo --at STATE_WRITE --epoch 2          # 安排故障（抛异常）
$CP fault  --data /tmp/demo --at COMMIT_RENAME --epoch 2 --halt # 安排故障（真杀 JVM）
$CP disarm --data /tmp/demo
$CP serve  --data /tmp/demo --port 8080
```

HTTP JSON 服务：

```bash
curl -X POST localhost:8080/events -H 'Content-Type: application/json' -d '{"key":"a","value":3}'
curl -X POST localhost:8080/drain                       # 消费并提交全部
curl -X POST localhost:8080/checkpoints                 # 显式屏障
curl -s   localhost:8080/status                         # 当前偏移/汇总
curl -X POST localhost:8080/faults -H 'Content-Type: application/json' \
     -d '{"at":"STATE_WRITE","epoch":2,"halt":false}'
curl -X DELETE localhost:8080/faults
```

更多请求样例见 `examples/requests/`。

---

## 4. 验收：连续执行 vs 恢复执行

样例 10 条事件（`examples/events.json`），`--every 5`：epoch 1 覆盖偏移 0..4，epoch 2 覆盖 5..9。
无故障基线：

```
committedOffset=9   sums={a:22, b:15, c:18}
```

在 epoch 2 的每个阶段注入一次性故障，随后重启恢复：

| 故障点 | 崩溃后表（恢复前） | 恢复后偏移 | 恢复后汇总 | 与基线一致 |
|---|---|---|---|---|
| PENDING_WRITE  | epoch1 / off=4 | 9 | {a:22,b:15,c:18} | ✅ |
| STATE_WRITE    | epoch1 / off=4 | 9 | {a:22,b:15,c:18} | ✅ |
| COMMIT_RENAME  | epoch1 / off=4 | 9 | {a:22,b:15,c:18} | ✅ |
| TABLE_APPLY    | epoch1 / off=4 | 9 | {a:22,b:15,c:18} | ✅ |
| AFTER_COMMIT   | epoch2 / off=9 | 9 | {a:22,b:15,c:18} | ✅ |

- 崩在 1/2：epoch 2 既未提交状态也未提交输出 → 作废，从偏移 5 重放，**不丢不重**。
- 崩在 3：状态已在、输出未提交 → 恢复补提交，表只应用一次。
- 崩在 4：输出已提交、表未应用 → 恢复幂等补应用。
- 崩在 5：一切已完成 → 恢复是空操作。

`Runtime.halt` 真杀进程与抛异常两种方式结果相同（见 `RUNLOG.md`）。
自动化测试还覆盖：连续 3 个 epoch 崩溃、同一 epoch 在两点先后崩溃、外部副作用重复演示、
虚拟时钟调度、HTTP 故障恢复。

---

## 5. 为什么本地汇总表能做到“无重复提交”

`table.json` 的应用是**用 epoch 携带的完整快照做一次原子重写**，并在同一次写入里推进
`appliedEpoch`：

- 崩溃后表要么停在旧 epoch（恢复时再应用一次，写入内容与上次完全相同 → 幂等），
  要么已是新 epoch（恢复时 `id <= appliedEpoch` 直接跳过）；
- 应用条件是按 epoch 升序 + `appliedEpoch` 去重，因此同一 epoch 的输出不会被应用两次。

这本质上是“目标存储支持幂等覆盖 + 提交与位点在同一原子单元”的事务性 sink。

---

## 6. 适用边界：不能推广到任意外部副作用

第 4/5 节的 exactly-once **只**成立于“提交目标是我们能用一次原子 rename 重写的本地文件”。

对**任意外部副作用**——发 HTTP webhook、发邮件/短信、调用支付、写无法回滚的第三方系统——
本协议**不**提供 exactly-once。原因在事务顺序里是结构性的：若把外部调用放在本地提交点
**之前**，崩溃恢复为了补完该 epoch 会**再调用一次**；放在提交点**之后**，又可能出现
“本地已提交、外部调用未发出”的丢失。两阶段中没有任何一个点能同时避免“重复”与“丢失”。

`UnsafeSideEffectTest` 明确演示这一点：在 COMMIT_RENAME 前崩溃时，外部副作用计数为 **2**
（崩溃前 1 次 + 恢复重试 1 次），而本地汇总表仍是 exactly-once。

要在真实系统中获得端到端 exactly-once，必须由**外部系统本身**提供以下能力之一（本项目不模拟）：

- **幂等键 / 去重令牌**：重试携带稳定的 epoch 级幂等 ID，外部系统按 ID 去重；
- **事务性 sink / 两阶段提交（2PC）**：外部存储参与同一提交协议；
- 或接受 at-least-once 并在消费端做去重。

换言之：**检查点协议能保证“重放同一批输入时本地状态一致”，但不能替外部世界实现幂等。**

---

## 7. 故障注入的建模说明

- 规则持久化在 `faults/injected.json`（`at`/`epoch`/`halt`），因此跨进程也生效；
- **一次性**：触发后规则即删除，模拟“机器重启后不会无限死在同一条指令上”；
- `halt=false`：抛 `InjectedCrash`（进程内测试，文件保留在崩溃现场）；
- `halt=true`：`Runtime.halt(99)`，不运行 shutdown hook、不 flush，最接近 `kill -9`。
- 崩溃安全粒度：实现对数据文件做 `force(fsync)`、提交走原子 rename，并尽力 fsync 目录；
  参考实现关注**进程级**故障（kill -9）。对“掉电 / 存储介质损坏”这类硬件级故障，
  正确性还依赖文件系统与磁盘的写序保证，超出本原型范围。

---

## 8. 局限与可扩展方向

- 单线程、单分区、内存完整快照：面向“小数据精确参考”，不是生产级高吞吐设计；
- 事件 schema 固定为 `(key, value:long)`；算子接口可扩展到其它确定性聚合；
- epoch 输出为完整快照 + 逐条 delta（审计/演示用），生产中可改为状态后端 + changelog；
- 没有保留多个历史检查点（仅 latest）；可扩展为带版本的检查点与清理策略。
