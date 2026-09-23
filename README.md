# hllengine —— 可合并的近似去重查询引擎（纯后端）

一个**单机、内存、零第三方依赖**的 Java 后端，用 HyperLogLog 做**可合并的近似基数估计**（近似去重计数），
并提供一个手写的内存查询引擎（scan / filter / project / group-by / 聚合）和 **JSON 请求入口**。

- 核心运算全部自己实现，**不调用任何现成 SQL 引擎**；
- 哈希算法、精度固定并写入每个草图，**合并强制要求配置一致**；
- 所有基数结果都明确标注为**估计值**，并附带名义相对标准误差，绝不冒充精确计数；
- 数据、逻辑/物理执行计划、草图都可以 JSON / 二进制导出与再导入。

无前端。仅需 JDK 17+（本仓库在 JDK 21 上开发与验证）。

---

## 1. 构建与运行

不需要 Maven / Gradle / 联网，纯 `javac` + `jar`：

```bash
./build.sh                 # 编译并打包出 hllengine.jar
java -jar hllengine.jar --help
```

一条命令完成「构建 + 全部测试 + 精度实验」并把输出写入 `reports/validation.log`：

```bash
bash tests/run_all.sh
```

### 入口形式

```bash
# 从 stdin 读一个 JSON 请求
echo '{"op":"listSketches"}' | java -jar hllengine.jar

# 从文件读
java -jar hllengine.jar --file examples/01-sketch-quickstart.json --pretty

# 内置单元测试（63 例）
java -jar hllengine.jar selftest

# 固定种子误差分布实验（默认 40 次/组，输出到 reports/）
java -jar hllengine.jar experiment --trials 40 --out-dir reports
```

响应统一为 JSON：

- 成功：`{"ok":true,"hashId":"MURMUR3_X64_128","result":{...}}`
- 失败：`{"ok":false,"error":{"code":"...","message":"..."}}`
- 退出码：`0` 成功，`1` 测试失败，`2` 请求/格式错误，`3` 内部错误。

---

## 2. 核心概念：估计值，不是精确计数

每个 HLL 基数结果都长这样（字段名刻意把「这是估计」写明白）：

```json
{
  "estimatedCardinality": 1502,
  "estimatedCardinalityRaw": 1501.7,
  "isEstimate": true,
  "isExact": false,
  "nominalRelativeStandardError": 0.01625,
  "nominalRelativeStandardErrorPercent": 1.625,
  "observedCount": 3000,
  "empty": false,
  "zeroRegisters": 412,
  "registers": 4096
}
```

- `estimatedCardinality` / `...Raw`：HLL 的**估计**基数（取整 / 原始值）；
- `nominalRelativeStandardError = 1.04 / √m`：估计量的**先验相对标准差**，是误差的**尺度**，
  既不是对单次估计的保证区间，也不是实测误差；
- `observedCount`：插入值的个数（流长度，**重复会重复计数**），与估计基数是两回事；
- 查询结果里每个近似列还会带一个兄弟列 `<别名>_relativeStandardError`，并在
  `approximateColumns` 中登记，消费方无需靠命名猜测。

若需要精确值（小数据量），用 `count_distinct`，它基于集合，返回精确数；`hll_distinct` 永远是近似。

### 固定的哈希与精度（合并契约）

- 哈希函数：**MurmurHash3，x64_128 变体**（`hashId = "MURMUR3_X64_128"`，本构建唯一支持、冻结），
  只用其 64 位 `h1`。实现通过 SMHasher 官方验证常量 `0x6384BA69` 与一份独立 Python 移植双重核对。
- 精度 `p ∈ [4,18]`，寄存器数 `m = 2^p`；默认 `p=12`（4096 寄存器，名义 σ≈1.625%）。
- 哈希种子 `seed` 也是配置的一部分（默认 0）。
- **合并要求配置完全一致**：`precision`、`hashId`、`seed` 三者必须相同，否则返回
  `INCOMPATIBLE_CONFIG` 且报错信息指出是哪一项不同。
- 插入值带**类型标签**后再哈希：数字 `1`、字符串 `"1"`、布尔 `true`、`null` 互不碰撞。

---

## 3. JSON 请求参考（op 列表）

草图类：

| op | 说明 |
|----|------|
| `createSketch` | `{name, precision?, seed?, hashId?}` |
| `add` / `addAll` | 插入单个 / 一批值 |
| `estimate` | 取估计结果（含误差标注） |
| `mergeSketches` | `{target, sources:[...]}`，要求配置一致，目标不能出现在 sources |
| `exportSketch` / `importSketch` | 导出/导入（base64 二进制或自描述信封） |
| `listSketches` / `dropSketch` | 列出 / 删除 |

数据集与查询类：

| op | 说明 |
|----|------|
| `createDataset` / `appendRows` / `getDataset` / `listDatasets` / `dropDataset` | 内存表管理 |
| `query` | 执行计划 |
| `explain` | 只导出逻辑/物理计划，不执行 |

其它：`batch`（顺序执行多个请求，`continueOnError` 控制遇错是否继续）。

### 查询计划

线性流水线：`Scan → Filter? → Project? 或 Aggregate? → Limit?`。

- 过滤算子：`eq, ne, lt, le, gt, ge, isnull, notnull, in, nin, like`（子串包含），
  以及 `and / or / not` 组合；
- 聚合函数：`count`、`count_distinct`（**精确**）、`hll_distinct`（**近似**，可带 `precision/seed`）、
  `sum, avg, min, max`；
- 支持 `groupBy`。

例：按国家分组，同时给出精确 UV 与近似 UV：

```json
{"op":"query","plan":{
  "dataset":"events",
  "filter":{"op":"ge","field":"age","value":18},
  "aggregate":{"groupBy":["country"],"aggregates":[
    {"fn":"count","alias":"events"},
    {"fn":"count_distinct","field":"uid","alias":"uv_exact"},
    {"fn":"hll_distinct","field":"uid","precision":12,"alias":"uv_hll"}
  ]}
}}
```

### 分片合并（典型近似去重场景）

各分片各自建草图 → 导出 → 在汇总端导入并 `mergeSketches`。合并取寄存器逐位最大值，
天然处理跨分片重复键；重复合并且幂等。配置不同则被拒绝。见
`examples/02-shard-merge.json` 与 `examples/bad/`。

---

## 4. 草图二进制格式（v1，可移植、可校验）

`exportSketch` 的 `serialized` 字段是 base64，底层小端/大端约定如下（多字节整数为**大端**）：

```
magic "HLL1"(4) | version=1(1) | flags=0(1) | precision(1)
| seed int32 BE(4) | hashIdLen uint16 BE(2) | hashId UTF-8
| observedCount int64 BE(8) | registers[m](每寄存器 1 字节)
| crc32 uint32 BE(4)   // 对前面所有字节
```

反序列化逐项校验：magic、version、flags、precision 范围、hashId、长度、
寄存器秩的合法上限（`64-p+1`）、非负计数、以及 CRC32。任何不符都返回 `BAD_FORMAT`。

---

## 5. 目录结构

```
src/hllengine/
  json/Json.java            严格 JSON 解析/序列化（零依赖，同时充当坏格式检查器）
  hll/MurmurHash3.java      冻结的哈希（Murmur3 x64_128）
  hll/HllConfig.java        精度/种子/hashId + 兼容性检查
  hll/HllSketch.java        HLL 草图：插入/合并/估计/序列化
  hll/HllEstimate.java      估计结果（显式误差标注）
  hll/ValueCoding.java      带类型标签的取值编码
  engine/                   Dataset / Predicate / AggSpec / LogicalPlan / PhysicalPlan / QueryResult
  server/Engine.java        请求分发与内存目录
  server/Main.java          JSON 入口（stdin/--file/selftest/experiment）
  test/                     自带测试框架 + 63 个用例 + 固定种子精度实验
examples/                   请求样例（examples/bad/ 为各类坏输入）
tests/                      CLI 端到端脚本 + 一键验证
reports/                    测试与误差实验产物
build.sh                    javac + jar 构建
```

## 6. 测试与如实结果

- 63 个内置单元测试（`selftest`）覆盖：空集、重复插入、类型标签、分片/多路合并、
  合并配置不兼容、十余种坏二进制格式、坏 JSON、查询引擎各算子、端到端 API；
- 16 个真实 JVM 进程的 CLI 端到端测试（`tests/cli_e2e.sh`），含跨进程导出/导入；
- 固定种子、多组基数（0…1,000,000）× 多精度（p=10/12/14）× 每组 40 次独立试验（共 960 次）
  的误差分布实验，产物见 `reports/accuracy_report.md` 与两个 CSV。

实测命令、结果与过程中发现并修复的问题，如实记录在 **[RUN_REPORT.md](RUN_REPORT.md)**。
