# 检查点与输出事务（Checkpoint & Output Transactions）

纯后端、零外部依赖的**事件流计算库 + JSON 输入输出服务**。演示并验证在“状态写入”与
“输出提交”各环节发生故障后，如何通过检查点协议做到：

- **输入偏移（input offset）精确一次地推进**；
- **算子状态（operator state）**随检查点一致快照与恢复；
- 对**受控的本地汇总表**实现**恢复后无重复提交（effectively exactly-once）**；
- 明确说明该保证**不能推广到任意外部副作用**（见下文“语义边界”）。

时间与调度（屏障触发）均可注入；无外部消息系统（Kafka/Pulsar 等），输入是本地
持久 JSONL 日志。小数据、确定性、可逐字节核对的参考实现。

---

## 1. 构建与运行

仅需 JDK 17+（在 JDK 21 上验证）。无 Maven/Gradle、无第三方库。

```bash
./build.sh        # javac 全量编译到 build/
./test.sh         # 运行全部自动化测试（退出码非 0 即有失败）
./acceptance.sh   # 跨独立 JVM 进程的故障注入/恢复验收（含与基线 diff）
./http-smoke.sh [port]   # 真实 HTTP curl 流程冒烟（崩溃→恢复）
```

命令行：

```bash
# 启动 JSON HTTP 服务
java -cp build com.example.cptx.Main serve --dir data/svc --port 8080 --every 5

# 正常载入 / 生成连续执行基线
java -cp build com.example.cptx.Main baseline --dir data/base --events examples/events.json --every 5

# 在某阶段“崩溃”（进程退出码 42，模拟进程死亡，内存全部丢失）
java -cp build com.example.cptx.Main crash --dir data/run --events examples/events.json \
     --every 5 --fault OUTPUT_COMMIT:4

# 全新进程恢复（重放输入日志；--fault 可省略，省略=干净恢复）
java -cp build com.example.cptx.Main resume --dir data/run --every 5

java -cp build com.example.cptx.Main status --dir data/run
```

`--fault PHASE[:ID]`：`PHASE` 为故障点，`ID` 为检查点（事务）号，省略表示“下一次检查点即中”。
可重复给出 `--fault` 构造连续多故障。

---

## 2. HTTP API

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/healthz` | `RUNNING` / `CRASHED` |
| POST | `/events` | `{"events":[{"key":"A","value":1.5}, ...]}`：先持久化输入日志（fsync），再处理 |
| POST | `/checkpoint` | 强制在当前偏移做一次检查点 |
| GET | `/status` | 最终偏移、检查点号、已提交事务集合、汇总表 |
| GET | `/summary` | 仅汇总表（崩溃时返回 503） |
| POST | `/faults` | `{"phase":"OUTPUT_COMMIT","checkpointId":3}`：一次性故障注入 |
| POST | `/recover` | 模拟进程重启：丢弃暂存、从最近**完整**检查点恢复、重放日志并提交尾部 |

请求样例见 [`examples/`](examples/)：

- [`request-post-events.json`](examples/request-post-events.json)
- [`request-arm-fault.json`](examples/request-arm-fault.json)
- [`request-empty.json`](examples/request-empty.json)
- [`events.json`](examples/events.json)：37 条确定性事件（CLI 验收用）

崩溃后服务返回 500 并进入 `CRASHED`：内存中的 Pipeline 被丢弃，后续写请求返回 503，
必须 `POST /recover`（等价于 CLI 里启动一个全新 JVM）。

---

## 3. 核心抽象

| 类 | 职责 |
|---|---|
| `InputLog` | 持久输入日志 `input.log`（JSONL，append-fsync）。行号即偏移：第 N 行 offset=N（0 基） |
| `KeyedAggregate` | 带状态算子：按 key 维护 `count/sum`；`snapshot/restore` |
| `CheckpointStore` | 检查点文件 `checkpoint-NNNNNN.json`（tmp+fsync+原子 rename），含 `nextOffset` 与算子状态 |
| `OutputLog` | 输出事务两阶段提交：`txn-N.staged` → 原子 rename → `txn-N.json`（已提交，不可变） |
| `SummaryTable` | 受控本地汇总表 `summary.json`：**始终由已提交事务日志重建**，原子重写 |
| `CheckpointPolicy` | 屏障调度，可注入：按条数 `count(n)`、按时间 `wallTime(clock,ms)`、`manual()` |
| `Clock` / `MutableClock` | 可注入时间；测试用时钟只显式推进 |
| `FaultSpec` / `FaultPhase` | 四个故障注入点，一次性、可指定检查点号 |
| `Pipeline` | 协调器，串起下面的协议与恢复 |

---

## 4. 检查点与输出事务协议

每次屏障（按条数/时间/手动）触发，`nextOffset` 为已处理输入条数。事务号 == 检查点号，
保证重放确定性：

```
① STATE_WRITE    算子状态 + nextOffset 快照写入 checkpoint-N.tmp（fsync）
                 → 原子 rename 成 checkpoint-N.json
② OUTPUT_STAGE   将本次检查点的输出快照写入 output/txn-N.staged（fsync，对外不可见）
③ OUTPUT_COMMIT  原子 rename：txn-N.staged → txn-N.json   ← 提交点
④ TABLE_APPLY    用“全部已提交事务”重建并原子重写 summary.json
```

四个注入点分别位于：①rename 之前、②提交之前、③提交之后但应用到汇总表之前、④汇总表重写之后。

### 恢复规则（关键）

由于 ① 先于 ③，崩溃可能发生在“状态已落盘但输出未提交”之间。因此恢复点不能只看
最新检查点文件，而要找最大的 **完整检查点 K**：

> `checkpoint-K.json` 与已提交的 `output/txn-K.json` **同时存在**。

- 编号更大的检查点文件是“输出未提交”的**孤儿**，删除后由重放确定性重建；
- 所有 `*.staged`（未提交输出）与 `*.tmp`（未完成快照）启动时丢弃；
- 从 `checkpoint-K.nextOffset` 恢复算子状态，重放 `input.log` 中 `offset ≥ nextOffset` 的事件；
- `summary.json` 永远以已提交事务日志 fold 重建，因此与内存无关、天然幂等。

### 为什么本地汇总表能恰好一次

- 汇总表从不追加增量，只做**按 key 的全量快照覆盖（upsert）**，重复应用同一已提交事务结果不变；
- 只有“已提交（rename 完成）”的事务才进入 fold；未提交的 staged 在恢复时被丢弃；
- 事务号确定性且与检查点绑定，重放不会产生新的/重复的事务号。

### 语义边界（不能推广到任意外部副作用）

本保证依赖两个前提：**(a)** 副作用对象是“可由本地提交日志确定性重建、且按 key 幂等覆盖”的
本地表；**(b)** 提交动作是本地文件系统上的原子 rename。

对**任意外部副作用不成立**，例如：发送邮件/短信、第三方支付扣款、调用不可重放的远程 API、
向不支持幂等键的系统写数据。在 ③ 提交与“外部动作真正生效”之间存在崩溃窗口：

- 若先提交后调用外部：崩溃恢复后重放会**重复**触发外部动作；
- 若先调用外部后提交：崩溃会使已发生的外部动作**无法由日志追溯**。

正确做法需要外部系统配合：稳定的**幂等键/去重 token**（本项目中即事务号）、外部侧的
**事务回执/两阶段登记**、或支持事务性提交的 sink（如支持事务的数据库/消息系统）。
本项目刻意不假装解决这一通用问题。

---

## 5. 验收方式与结果

### 自动化测试（`./test.sh`，同进程内）

- `CoreTest`：JSON、聚合算子、偏移推进、按条数/按**注入时钟**触发检查点；
- `RecoveryTest`：四个阶段 × 两个检查点位置、双故障、全部提交后崩溃、重复 reopen，
  共 288 项断言，逐项对比连续基线的 `nextOffset / lastCheckpointId / summary / 已提交事务集合`；
- `ServiceTest`：真实启动 HTTP 服务走网络：发送→注入故障→500/CRASHED→写拒绝 503
  →`/recover`→无重复→非法输入 400。

### 跨进程验收（`./acceptance.sh`）

每个阶段用一个 JVM `crash`（退出码 42），再用**另一个全新 JVM** `resume`，
把最终 status 的确定性字段与连续执行基线 `diff`。还包含：连续两次崩溃（跨三个进程）、
删除 `summary.json` 后由提交日志重建。

实测结果记录在 [`docs/TEST-RUN.md`](docs/TEST-RUN.md)（含实际命令、输出与退出码）。

---

## 6. 目录与数据布局

```
src/com/example/cptx/
  core/      流计算库与协议（无框架依赖）
  service/   JDK HttpServer JSON 服务
  tests/     零依赖测试运行器与用例
examples/    请求与事件样例
docs/        协议示意与实测记录
data/<run>/
  input.log
  checkpoint-000001.json …
  summary.json
  output/txn-000001.json …
```
