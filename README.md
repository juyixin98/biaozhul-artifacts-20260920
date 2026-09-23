# 确定性分组 TopK（deterministic-grouped-topk）

纯后端、零第三方依赖（仅 JDK 21）的单机内存分组 TopK 查询引擎。核心运算（分组、
排序、TopK、中间状态合并）全部自行实现，不调用任何现成 SQL 引擎。

## 语义

- 每行数据：`group`（分组键）、`value`（排序值）、`seq`（加载时分配的全局唯一序号）。
- 排名为**全序**：`value DESC, seq ASC`。并列值由序号决定先后，因此结果与分片
  数量、合并顺序完全无关。
- `k = 0`：合法，每组返回空列表（组仍出现在结果中）。
- `k < 0`：非法，查询被拒绝（HTTP 400 / IllegalArgumentException）。
- `k > budget`：非法，超出每组内存预算，查询被拒绝。

## 执行模型

1. 数据按加载顺序轮询切成 `shards` 个分片；
2. 每个分片产出**可合并中间状态** `PartialState`：每组保留排名最高的至多 k 行；
   - 组内行数 ≤ 预算：内存全排序后取前 k（`in-memory-sort`）；
   - 组内行数 > 预算：大小为 k 的有界堆流式处理（`bounded-heap`），内存 O(k)；
   - 两种策略结果完全一致（全序保证）；
3. 按 `mergeOrder`（缺省为自然顺序）逐个合并中间状态。合并即“并集取前 k”，
   满足结合律与交换律，**合并顺序不影响最终结果**。

数据（含分配的 seq）与执行计划（分片、每组策略、合并顺序）均可导出为 JSON。

## 构建与测试

```bash
javac -d build $(find src test -name '*.java')
java -cp build topk.TestRunner
```

## CLI

```bash
java -cp build topk.Main --data data/sample.json --k 2 --shards 4 \
     [--merge-order 2,0,3,1] [--budget 1000] \
     [--export-plan plan.json] [--export-data snapshot.json]
```

## HTTP 服务（JSON 入口）

```bash
java -cp build topk.Server --port 8080 --budget 1000
```

| 端点 | 方法 | 说明 |
|---|---|---|
| `/load` | POST | `{"rows":[{"group":"a","value":5},...]}`，替换数据集 |
| `/append` | POST | 同上，追加 |
| `/query` | POST | `{"k":2,"shards":4,"mergeOrder":[2,0,3,1]}`（mergeOrder 可选） |
| `/export/data` | GET | 导出数据集（含 seq） |
| `/export/plan` | GET | 导出最近一次查询的执行计划 |

请求样例见 `examples/`：`load.json`、`query.json`、`query-merge-order.json`、
`query-negative-k.json`。试用：

```bash
curl -X POST --data @examples/load.json http://127.0.0.1:8080/load
curl -X POST --data @examples/query.json http://127.0.0.1:8080/query
curl http://127.0.0.1:8080/export/plan
```

## 自动化测试覆盖（`test/topk/TestRunner.java`）

- 随机数据集（小值域 → 大量并列）上遍历分片数 1..8 与随机合并顺序，对照全排序基线；
- k=0 返回空组；负 k、k 超预算、非法 mergeOrder 均被拒绝；
- 并列值按 seq 稳定裁决；
- 小预算强制 `bounded-heap` 策略且结果与全排序一致；
- 中间状态合并的结合律/交换律；空数据集；JSON 往返。

## 实际运行记录（2026-09-23，OpenJDK 21.0.12.1）

```
$ javac -d build $(find src test -name '*.java')   # 通过，无警告
$ java -cp build topk.TestRunner
```

首轮运行 **10 项中 3 项未通过**，均为真实缺陷并已修复：

1. `QueryEngine.heapTopK` 堆方向错误：用正序堆导致堆顶是最优元素，新行与最优
   元素比较并驱逐了最优元素。改为逆序堆（堆顶为当前 k 行中最差者）。
   对应失败：`budget forces heap, same result`、`sharding/merge-order sweep vs full sort`。
2. `k=0` 时分片计算不注册组，结果缺少空组，与全排序基线不一致。修复：
   `PartialState.add` 在 k=0 时注册空组，`computeShard` 增加 k=0 分支。
   对应失败：`k=0 yields empty groups`。
3. 测试自身缺陷：随机用例中 k 可能超过随机预算，触发合法的“k 超预算”校验。
   修正测试使 `k ≤ budget`。对应失败：`sharding/merge-order sweep vs full sort`
   （第二轮）。

修复后最终运行：

```
PASS json round trip
PASS k=0 yields empty groups
PASS negative k rejected
PASS k over budget rejected
PASS invalid merge order rejected
PASS ties broken by seq
PASS budget forces heap, same result
PASS merge is associative+commutative
PASS sharding/merge-order sweep vs full sort
PASS empty dataset
passed: 10, failed: 0
```

CLI 与 HTTP 端到端验证（均实际执行）：

- `topk.Main --data data/sample.json --k 2 --shards 3 --export-plan ... --export-data ...`
  输出正确结果，计划与数据快照成功导出；
- `--shards 4 --merge-order` 分别取 `0,1,2,3` / `3,2,1,0` / `2,0,3,1`，三次输出逐字节一致；
- HTTP：`/load` 载入 12 行；`/query`（含自定义 mergeOrder）结果与 CLI 一致；
  `{"k":-1}` 返回 `400 {"error":"k must be >= 0, got -1"}`；
  预算 5 时 `{"k":6}` 返回 `400 {"error":"k (6) exceeds per-group memory budget (5)"}`；
  `/export/plan`、`/export/data` 正常导出。

当前状态：**全部测试通过，无未通过项。**

## 目录结构

```
src/topk/Json.java          最小 JSON 解析/序列化（无依赖）
src/topk/Row.java           行与全序比较器（value DESC, seq ASC）
src/topk/DataSet.java       内存数据集、seq 分配、确定性分片
src/topk/TopKQuery.java     查询解析与校验（k、shards、mergeOrder）
src/topk/PartialState.java  可合并中间状态（每组至多 k 行）
src/topk/QueryEngine.java   分片执行、预算策略、有界堆、全排序基线
src/topk/ExecutionPlan.java 可导出执行计划
src/topk/Server.java        JSON-over-HTTP 入口（JDK 内置 HttpServer）
src/topk/Main.java          CLI 入口
test/topk/TestRunner.java   自动化测试（无依赖测试运行器）
data/sample.json            样例数据
examples/                   请求样例
```
