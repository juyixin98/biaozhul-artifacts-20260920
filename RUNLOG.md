# 运行记录（RUNLOG）

本文件记录在交付环境中的**实际**命令、结果与退出码。所有命令均在项目根目录
`/home/admin/Downloads/biaozhul/opp12/b` 下执行；除系统包安装外均可由仓库内脚本复现。

- 记录时间：2026-09-23（UTC）
- 系统：`Linux 6.8.0-90-generic x86_64 (Ubuntu 24.04)`
- JDK：环境装有 OpenJDK 17 与 21；默认 `java/javac` 解析到
  `java-21-openjdk-amd64`。构建脚本统一加 `--release 17`，在 17/21 上均可编译运行。

```text
$ java -version
openjdk version "21.0.12.1" 2026-08-18
$ javac -version
javac 21.0.12.1
```

## 1. 环境准备

初始环境无 JDK / Maven。安装命令（需 sudo）：

```bash
sudo apt-get install -y openjdk-17-jdk-headless
```

项目本身**不依赖 Maven/Gradle 或任何第三方库**，只用 JDK 自带的 `javac/java`。

## 2. 干净构建

```bash
$ rm -rf build out && ./build.sh
== compiling main sources ==
== compiling test sources ==
Build OK -> build/classes, build/test-classes
BUILD_EXIT=0
real	0m2.2s
```

## 3. 自动化测试（17 个测试类，全部通过）

```bash
$ ./test.sh
PASS  value equality/hash (numeric cross-type, type families)
PASS  composite key NULL never matches
PASS  basic INNER join, in-memory
PASS  LEFT join unmatched + null-key rows padded with NULLs
PASS  duplicates -> Cartesian product
PASS  empty left / empty right for INNER and LEFT
PASS  multi-column keys, NULL in every key position
PASS  randomized data: engine multiset == nested-loop oracle (in-mem + spill)
PASS  all-hot-key INNER: repartition cannot split, BNLJ fallback, full Cartesian
PASS  all-hot-key LEFT via fallback + BNLJ marks every probe row
PASS  mixed skew: hot key + uniform keys, fallback and split coexist
    (50k smoke took 411 ms, 6988900 spill bytes, 320 partitions)
PASS  50k x 50k unique-key INNER smoke (bounded memory)
PASS  uniform keys repartition into smaller partitions without fallback
PASS  disk quota exhausted raises DISK_BUDGET_EXHAUSTED and cleans up
PASS  JSON parse/write fidelity
PASS  explain/plan-only reports IN_MEMORY vs PARTITIONED_SPILL
PASS  CLI end-to-end: run, plan-only, DISK_BUDGET_EXHAUSTED exit code
------------------------------------------------------------
17/17 test classes passed
TEST_EXIT=0
real	0m9.3s
```

验证口径：除计数/模式断言外，随机数据、空表、NULL、全热点、混合倾斜等用例都把
引擎输出与 `NestedLoopJoin`（独立嵌套循环参考实现）做**多重集比对**（按 JSON
规范化的行内容计频次）。冒烟计时数字每次运行有波动（另一次干净运行为 355 ms /
567 ms），但远低于 60 s 阈值。

## 4. 样例请求实际运行

### 4.1 基础 INNER（内存模式，含 NULL 键被丢弃）

```bash
$ ./run.sh examples/inner-small.json ; echo exit=$?
... "mode": "IN_MEMORY", "outputRows": 3, "nullKeyLeftRows": 1,
    "nullKeyRightRows": 0, "spillBytesWritten": 0 ...
"rowCount": 3, "truncated": false
exit=0
```

结果：`order_id` 3 的键为 NULL 被丢弃；customer 20 有重复左行 → 2 条 +
customer 10 一条 = 3 条。

### 4.2 基础 LEFT（未匹配补 NULL）

```bash
$ ./run.sh examples/left-small.json ; echo exit=$?
"mode": "IN_MEMORY", "outputRows": 4
exit=0
```

输出行（右表三列，未匹配补 null）：

```text
[1, "a", 10, "a", 0.1]
[2, "b", 11, "b", 0.2]
[3, "a", 10, "a", 0.1]
[4, "c", null, null, null]
```

### 4.3 LEFT + 多列键 + 各位置 NULL，强制落盘（inMemoryRows=4）

```bash
$ ./run.sh examples/left-multikey-null.json ; echo exit=$?
"mode": "PARTITIONED_SPILL", "outputRows": 6,
"nullKeyLeftRows": 2, "nullKeyRightRows": 1,
"partitionsCreated": 4, "fallbackBlockNestedLoops": 0, "spillBytesWritten": 223
exit=0
```

输出行（注意 `(eng,null)` 与 `(null,eu)` 即使键看起来相同也不匹配，右表 `(eng,null)`
被丢弃）：

```text
[1, 'eng', 'us', 'Ada', 10, 'eng', 'us', 'A1']
[1, 'eng', 'us', 'Ada', 11, 'eng', 'us', 'A2']
[4, 'sales', 'us', 'Noah', 13, 'sales', 'us', 'B1']
[2, 'eng', null, 'Lin', null, null, null, null]
[3, null, 'eu', 'Mae', null, null, null, null]
[5, 'hr', 'ap', 'Ivy', null, null, null, null]
```

### 4.4 热点键：递归再分区拆不开 → 有界块嵌套循环回退

```bash
$ ./run.sh examples/hot-key-fallback.json ; echo exit=$?
"mode": "PARTITIONED_SPILL", "outputRows": 65,
"partitionsCreated": 9, "repartitions": 1,
"fallbackBlockNestedLoops": 1, "spillBytesWritten": 955
exit=0
```

8 个左行 × 8 个右行同属热点键 7 → 64 条笛卡尔积，外加键 1 的 1 条匹配，共 65。

### 4.5 外部 JSONL 表 + 结果/计划导出文件

```bash
$ ./run.sh examples/spill-file-input.json ; echo exit=$?
"mode": "PARTITIONED_SPILL", "outputRows": 10,
"nullKeyLeftRows": 1, "partitionsCreated": 4, "spillBytesWritten": 415
"output": {"path": ".../out/events-joined.jsonl", "format": "jsonl", "rows": 10}
"planOutput": ".../out/events-plan.json"
exit=0

$ cat out/events-joined.jsonl
[1,1,"login",1,1,"pro"]
[5,1,"click",1,1,"pro"]
[9,1,"purchase",1,1,"pro"]
[2,2,"login",2,2,"pro"]
[6,2,"click",2,2,"pro"]
[10,2,"purchase",2,2,"pro"]
[0,0,"login",0,0,"free"]
[4,0,"click",0,0,"free"]
[8,0,"click",0,0,"free"]
[11,0,"logout",0,0,"free"]
```

`user_id` 为 null 的事件 `[3,...]` 与不存在的 user 9 事件 `[7,...]` 在 INNER 中被排除。

### 4.6 只导出执行计划（不执行连接、不落盘）

```bash
$ ./run.sh plan examples/spill-file-input.json ; echo exit=$?
"mode": "PARTITIONED_SPILL", "fanout": 4
exit=0
```

### 4.7 磁盘额度耗尽（错误信封 + 退出码 2）

```bash
$ ./run.sh examples/disk-budget-exhausted.json ; echo "exit=$?"
{
  "ok": false,
  "error": {
    "code": "DISK_BUDGET_EXHAUSTED",
    "message": "Disk budget of 100 bytes exceeded while spilling (would use 105 bytes) at L0-4.jsonl"
  }
}
exit=2
```

测试套件中的 `DiskBudgetExhausted` 用例额外断言：异常前零输出，且运行产生的唯一
UUID 临时目录及其内全部文件均被清理（配置的 `spillDir` 父目录即使保留也为空）。

### 4.8 stdin 管道、跨类型数字等值、非法请求

```bash
$ echo '{"joinType":"INNER", ...左 [[1,"int"],[1.00,"dec"]], 右 [[1.0,"float"]] ...}' \
    | ./run.sh -
"outputRows": 2, "rowCount": 2          # 整数 1 与十进制 1.00 都匹配浮点 1.0

$ echo '{...引用不存在的列...}' | ./run.sh - ; echo "exit=$?"
{"ok":false,"error":{"code":"INVALID_REQUEST",
 "message":"Unknown column: missing (columns: [k])"}}
exit=2

$ echo 'not json' | ./run.sh - ; echo "exit=$?"
{"ok":false,"error":{"code":"INVALID_REQUEST",
 "message":"JSON parse error at position 0: Invalid literal"}}
exit=2
```

## 5. 验收项对照

| 验收要求 | 落实位置 / 证据 |
| --- | --- |
| 不调用现成 SQL 引擎完成核心运算 | 连接/哈希/分区/落盘/回退全部手写于 `HashJoinEngine.java`、`Spiller.java`；无第三方依赖 |
| INNER / LEFT | `JoinType` + 引擎两路径；样例 4.1–4.4、4.7 |
| 多列键含 NULL 语义 | `Key.hasNull()` 路由 + `Value.equalsValue`；样例 4.3；测试 2、7 |
| 超阈值分区落盘 | `mode=PARTITIONED_SPILL`、JSONL 成对分区；样例 4.3–4.5 |
| 热点分区继续拆分 / 有界回退 | 深度加盐递归再分区 + 双侧分块 BNLJ；样例 4.4；测试 9–11 |
| 重复键笛卡尔积 | 键桶列表 × 探测行；样例 4.4（64=8×8）；测试 5、9 |
| 嵌套循环参考实现比对多重集 | `NestedLoopJoin` + `Multiset`；测试 3–11 等 |
| 全热点键 | 测试 9（3000×400）、10（2500 热点 LEFT）、11（混合） |
| 空表 | 测试 6：四种左右空组合 × 两种连接 |
| NULL | 测试 2、4、7、8（随机 8% NULL）、10 |
| 磁盘额度耗尽 | 测试 14（错误码、零输出、清理）、样例 4.7 |
| 数据/执行计划可导出 | `output` / `planOutput` / `plan` 子命令；样例 4.5、4.6 |
| 源码、README、请求样例、自动化测试 | 全部在仓库中 |
| 实际运行并如实记录 | 本文件；未通过项见下 |

## 6. 未通过项 / 已知限制

- **最终状态：17/17 测试类全部通过，无未通过项。**
- 开发过程中出现过、已修复的问题（均为测试侧而非引擎缺陷）：
  1. 初版测试断言输出列下标写错（`id/rid` 列位置），修正后通过；
  2. 磁盘额度用例把已含 `/budget-spill` 的路径又经测试辅助函数追加 `/spill`，
     导致多出一个空的父目录被误判为泄漏，改为显式配置后通过。
- 已知非目标（README “限制” 节）：无前端/HTTP 服务；非等值连接、聚合、排序、
  投影裁剪不支持；内存按“行数阈值+分块”保证驻留有界，不做 JVM 堆字节级核算。
