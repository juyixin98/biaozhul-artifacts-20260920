# 实测记录（命令、结果、退出码）

记录时间：2026-09-23。环境：Ubuntu，OpenJDK 21.0.12，无 Maven/Gradle、无第三方依赖。
本文所有输出均为实际运行抓取；完整原始输出见同目录
[`acceptance-output.txt`](acceptance-output.txt) 与 [`http-smoke-output.txt`](http-smoke-output.txt)。

## 0. 环境

```
openjdk version "21.0.12.1" 2026-08-18
OpenJDK 64-Bit Server VM (build 21.0.12.1+1-1-24.04.4-Ubuntu)
```

## 1. 构建

命令：`./build.sh`（内部 `javac -d build @sources.txt`）

结果：`构建完成 -> build/`，退出码 **0**。

## 2. 自动化测试

命令：`./test.sh`

```
CoreTest: 通过 (21 项断言)
RecoveryTest: 通过 (288 项断言)
ServiceTest: 通过 (23 项断言)
==================================================
总计: 332 项断言，0 项失败
```

退出码 **0**。

- **CoreTest**：JSON（中文/转义/往返）、键控聚合、偏移推进与按条数检查点、
  用 `MutableClock` 驱动的**按时间**检查点策略（验证时间可注入）。
- **RecoveryTest**：四个故障阶段分别在检查点 **3** 与 **7** 崩溃后用“新对象”恢复
  （等价新进程，复用磁盘、不共享内存）；双故障 `STATE_WRITE@4 → OUTPUT_COMMIT@6`；
  全部输出提交后 `TABLE_APPLY@8` 崩溃；无故障重复 reopen 不重复计数。
  每项都把 `nextOffset / lastCheckpointId / summary / 已提交事务集合`
  与连续执行基线逐字段比较，并校验崩溃现场与恢复后的临时文件状态。
- **ServiceTest**：真实启动 JDK HTTP 服务，走 localhost TCP：发事件、注入故障、
  收到 500 进入 CRASHED、崩溃后写入被 503 拒绝、`/recover` 后偏移与汇总无重复、
  非法输入 400。

## 3. 跨独立 JVM 进程验收

命令：`./acceptance.sh`（数据 `examples/events.json`，37 条，每 5 条一个检查点，
崩溃点统一为检查点 4；崩溃与恢复分别在不同 JVM 进程，崩溃进程退出码 42）。

退出码 **0**，汇总：`验收结果：通过 6 项，失败 0 项`。

| # | 场景 | 崩溃退出码 | 恢复后与基线 | 事务集合 | 临时文件 |
|---|---|---|---|---|---|
| 1 | 连续执行基线 | — | — | 1..8 | 无 |
| 2a | `STATE_WRITE@4` 崩溃→恢复 | 42 | 完全一致 ✓ | 1..8 ✓ | 无残留 ✓ |
| 2b | `OUTPUT_STAGE@4` 崩溃→恢复 | 42 | 完全一致 ✓ | 1..8 ✓ | 无残留 ✓ |
| 2c | `OUTPUT_COMMIT@4` 崩溃→恢复 | 42 | 完全一致 ✓ | 1..8 ✓ | 无残留 ✓ |
| 2d | `TABLE_APPLY@4` 崩溃→恢复 | 42 | 完全一致 ✓ | 1..8 ✓ | 无残留 ✓ |
| 3 | 双故障（跨 3 进程）`STATE_WRITE@3 → OUTPUT_COMMIT@6 → 恢复` | 42 / 42 | 完全一致 ✓ | 1..8 ✓ | 无残留 ✓ |
| 4 | 删除 `summary.json` 后重启，由提交日志重建 | — | 完全一致 ✓ | — | — |

### 连续执行基线（归一化 status）

```json
{
  "committedTxns": [1, 2, 3, 4, 5, 6, 7, 8],
  "lastCheckpointId": 8,
  "nextOffset": 37,
  "summary": {
    "A": { "count": 13, "sum": 12.0 },
    "B": { "count": 12, "sum": 12.5 },
    "C": { "count": 12, "sum": 11.0 }
  }
}
```

该汇总值经**独立于被测代码**的 `jq` 直接对原始事件聚合交叉核对，结果一致：

```bash
$ jq '[group_by(.key)[] | {key:.[0].key, count:length, sum:(map(.value)|add)}]' examples/events.json
[ {"key":"A","count":13,"sum":12},
  {"key":"B","count":12,"sum":12.5},
  {"key":"C","count":12,"sum":11} ]
```

### 崩溃现场（恢复之前）

- `STATE_WRITE@4`：留下 `checkpoint-000004.json.tmp`（rename 前死亡）；
- `OUTPUT_STAGE@4`：留下 `output/txn-000004.staged`（提交前死亡）；
- `OUTPUT_COMMIT@4` / `TABLE_APPLY@4`：无暂存文件（rename 已完成）。

恢复后以上文件全部被清理或回滚重建，最终磁盘只含 `checkpoint-1..8.json`、
`output/txn-1..8.json`，且与基线逐字节同构。

## 4. HTTP 服务冒烟（真实 curl）

命令：`./http-smoke.sh 19153`（完整输出见 `http-smoke-output.txt`），退出码 **0**。关键片段：

```
POST /events (5条)      -> {"accepted":5,"firstOffset":0,"lastOffset":4,"cp":1,"off":5}
再发 7 条               -> {"accepted":7,"cp":2,"off":12}
POST /faults OUTPUT_COMMIT@3
POST /events (5条)      -> {"error":"INJECTED_CRASH","phase":"OUTPUT_COMMIT","checkpointId":3}  (HTTP 500)
GET  /healthz           -> {"ok":false,"state":"CRASHED"}
崩溃后写入               -> HTTP 503（被拒绝，未进入系统）
POST /recover           -> {"recovered":true,"resumedToOffset":17,"cp":4,"off":17,"Zpresent":false}
GET  /summary           -> A{9,9.0} B{7,5.5} C{1,3.0}   # 崩溃窗口内 2 条 B 由重放补回，拒绝的 Z 不存在
非法输入                 -> 空 events HTTP 400 / 非法 JSON HTTP 400
```

注意 `resumedToOffset:17`：崩溃发生在偏移 15（第 3 个检查点边界），其后已写入输入日志的
2 条 B 在恢复时被重放并由尾部检查点（cp=4）提交，因此最终 B 的 count=7、sum=5.5，
与连续处理这 17 条输入的结果一致；崩溃后被 503 拒绝的键 Z 从未进入。

## 5. 未通过项 / 已知限制

- 当前版本下，上述自动化测试（332 项断言）、跨进程验收（6 项）、HTTP 冒烟**全部通过，无未通过项**。
- 开发过程中曾出现并已修复的真实问题（保留以说明协议的必要性）：
  1. 恢复最初只信任“最新检查点文件”，在 STATE_WRITE 之后/OUTPUT_COMMIT 之前崩溃时
     会跳过未提交事务 → 改为只认“检查点文件 **且** 已提交事务同时存在”的**完整检查点**；
  2. 检查点曾写两个临时文件导致 `.tmp` 残留 → 统一为“写一个 fsync tmp 再原子 rename”；
  3. HTTP `/recover` 最初未提交尾部片段，导致不足一个检查点间隔的尾部只在内存 →
     恢复时补一次 `flushCheckpoint()`。
- 非目标/已知限制：本实现是**单分区、小数据、单机文件系统**参考实现；未做（也明确不承诺）
  对任意外部副作用的恰好一次，需要外部系统自身的幂等键/事务回执（见 README“语义边界”）。
