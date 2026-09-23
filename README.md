# 可合并近似去重引擎（HyperLogLog，纯后端）

单机内存的**近似基数（去重计数）**查询引擎，JSON 文件为请求入口。
核心运算全部自行实现，**不使用任何 SQL 引擎或第三方基数库**（连 JSON 解析都是自研的零依赖实现）。
草图（sketch）与执行计划均可导出/导入，支持多分片合并。无任何前端代码。

> ⚠️ **关于“计数”的性质**：本系统输出的一切去重数字都是 HyperLogLog 的**估计值（estimate）**，
> 不是精确计数。响应中始终带 `"exact": false`、相对标准误差（RSE）和名义置信区间。
> 只有空集（估计为 0）在语义上是确定的。HLL 不保存原始元素，无法回溯精确集合。

## 1. 目录结构

```
src/main/java/dev/dedup/hll/
  Murmur3Hash128.java       固定哈希：MurmurHash3 x64_128（固定种子，只用 h1）
  HllConfig.java            精度 p + 哈希标识，不可变配置与兼容性检查
  HllSketch.java            HLL 草图：插入、估计、逐寄存器 max 合并
  BinarySketchCodec.java    紧凑二进制格式（魔数/版本/CRC32/严格校验）
  SketchJsonCodec.java      可移植 JSON 草图格式
  ValueCanonicalizer.java   JSON 值 -> 带类型标签的确定性字节
  Json.java                 零依赖 JSON 解析器/写入器
  QueryEngine.java          内存查询引擎：顺序执行算子、错误处理、执行计划
  Main.java                 CLI：java dev.dedup.hll.Main [request.json]
  ErrorDistributionMain.java 固定种子多基数误差分布实验
src/test/java/dev/dedup/hll/  零依赖测试框架 + 4 个测试类（26 万+ 断言）
samples/requests/             5 个请求样例
samples/responses/            实际运行得到的响应 + 执行计划
results/hash_kat_reference.py 独立 Python 哈希参考实现（KAT 向量生成器）
results/hash-kat-vectors.json 哈希已知答案向量
results/error-distribution/   误差分布报告（json + md，固定种子可复现）
build.sh / test.sh / run-samples.sh / run-error-distribution.sh
```

## 2. 算法与固定参数（版本内不可更改）

| 项目 | 取值 |
|---|---|
| 哈希算法 | MurmurHash3 **x64 128-bit**（Austin Appleby 公有领域实现） |
| 种子 | **固定** `0x9747B28C`（不接受调用方覆盖） |
| 消费位 | 仅用 128 位输出的低 64 位 **h1** |
| 算法标识串 | `MURMUR3_X64_128_SEED9747B28C_H1`（写入序列化格式并参与兼容性检查） |
| 精度 p | 4..18，寄存器数 m=2^p，草图固定占用 m 字节 |
| 索引/rank | h1 高 p 位为寄存器索引；剩余位 `rank = clz((h1<<p)|1<<(p-1))+1`，范围 1..65-p |
| 合并 | 同配置草图逐寄存器取最大 rank（天然支持分片重叠，幂等、可交换、可结合） |
| 基数估计 | 小基数线性计数 `m·ln(m/V)`（阈值 raw≤2.5m 且有零寄存器）；否则调和平均 α·m²/Σ2^-R |
| 理论误差 | 经典相对标准误差 RSE ≈ 1.04/√m。p=12 时 m=4096，**RSE ≈ 1.625%** |

**有意未实现并如实声明**：HLL++ 的经验偏差修正（empirical bias correction）与大基数量程修正。
本实现用经典线性计数覆盖小基数区间；不做偏差修正对 p≥10 的常规使用影响很小，
但在极小基数/极低精度下与 HLL++ 调优实现会有差异。64 位哈希下大基数量程修正无实际意义，故省略。

### 值的规范化（为什么字符串 "1" 和数字 1 不会被当成同一个）

插入前先加 **1 字节类型标签**再哈希：

| JSON 值 | 规范化字节 |
|---|---|
| `null` | `00` |
| `false` / `true` | `01` / `02` |
| 整数 | `03` + 十进制 ASCII（如 `1`、`-3`） |
| 浮点 | `04` + `BigDecimal.stripTrailingZeros().toPlainString()`（`1.0`、`1.00`、`1e2` 规范化一致） |
| 字符串 | `05` + UTF-8 字节 |

对象和数组不允许作为去重元素（直接报错），避免定义不清的序列化歧义。

## 3. 构建与测试（零依赖，仅需 JDK）

开发环境为 OpenJDK 21（代码使用 Java 8 级 API，未依赖 21 特性）。

```bash
./build.sh                    # 编译到 build/classes
./test.sh                     # 编译 + 运行全部自动化测试
./run-samples.sh              # 跑 samples/ 下请求，响应写入 samples/responses/
./run-error-distribution.sh   # 固定种子误差实验 -> results/error-distribution/
```

手工方式：

```bash
find src/main/java -name '*.java' > /tmp/src.txt
javac -encoding UTF-8 -d build/classes @/tmp/src.txt
java -cp build/classes dev.dedup.hll.Main samples/requests/01-basic-add-estimate.json
```

## 4. JSON 请求协议

顶层 `{"ops":[ ... ]}`，算子在数组中**顺序执行**，草图保存在内存 Map。
某个算子失败则在该算子处中止，返回 `ok=false`、错误码与 `failedOpIndex`（之前算子已生效）。

| 算子 | 必填字段 | 说明 |
|---|---|---|
| `create` | `name`,`precision` | 新建空草图 |
| `add` | `name`,`values` | 规范化 + 固定哈希 + 更新寄存器（重复幂等） |
| `add-hash` | `name`,`hashes` | 直接注入 64 位 h1（十进制或 `0x..`），供确定性测试 |
| `merge` | `name`,`sources`(≥2) | 兼容性检查通过后逐寄存器 max；源可重叠 |
| `estimate` | `name` | 返回估计值、`exact:false`、RSE、68%/95% 区间、零寄存器数 |
| `export` | `name`,`format` | `binary`（Base64 的 HLCD v1）或 `json`（HLL-JSON v1） |
| `load` | `name`,`format` | 与 export 对应，严格校验后载入内存 |
| `list` | — | 列出内存中草图（配置/命中寄存器数，不含估计） |

响应固定包含 `ok / opsProcessed / results / executionPlan / metadata`，
其中 `executionPlan` 是可读的执行计划（每步算子与实际行为描述），也可用 CLI 的
`--plan-out 文件` 单独导出。

### 错误码

| code | 触发条件 |
|---|---|
| `MALFORMED_JSON` | 请求读不懂（JSON 语法错误、顶层非对象），CLI 退出码 2 |
| `INVALID_REQUEST` | 缺字段、精度越界、空 values、未知导出格式等 |
| `UNKNOWN_OP` | 无法识别的算子 |
| `SKETCH_NOT_FOUND` / `SKETCH_EXISTS` | 草图不存在 / 重名创建 |
| `INCOMPATIBLE_SKETCH` | 合并双方 **p 或 hashId 不一致**（拒绝合并且不修改任何草图） |
| `BAD_FORMAT` | 导入草图魔数/版本/长度/寄存器值域/标志位/**CRC32** 任一校验失败 |

CLI 退出码：`0` 成功；`1` 业务错误（响应 `ok=false`）；`2` 请求无法读取/解析。

## 5. 二进制草图格式（HLCD v1，大端序）

```
偏移  长度 字段
0     4   魔数 'H' 'L' 'C' 'D'
4     1   版本 = 1
5     1   精度 p（4..18）
6     1   哈希标识字节长度 L（1..255）
7     1   标志位（v1 必须为 0）
8     4   CRC32，对偏移 12 之后全部字节计算
12    L   哈希标识 UTF-8（内容即固定算法+种子串）
12+L  m   m=2^p 个寄存器字节，值域 0..(65-p)
```

合并的**配置一致性**要求（见 `HllConfig.incompatibilityReason`）：

1. 精度 p 必须相同（寄存器数不同无法逐位合并）；
2. 哈希标识必须相同（不同哈希算法/种子下同一值映射到不同桶，合并结果无意义）。

不做静默降精度、不做重哈希；不一致直接 `INCOMPATIBLE_SKETCH`。

## 6. 请求样例

| 文件 | 演示内容 | 退出码 |
|---|---|---|
| `01-basic-add-estimate.json` | 建草图、重复值/跨类型插入、估计 | 0 |
| `02-shard-merge.json` | 3 个地域分片（含重叠用户）合并为全局 UV | 0 |
| `03-incompatible-merge.json` | p=12 与 p=10 草图合并被拒绝 | 1（预期） |
| `04-export-binary-json.json` | 同一草图导出二进制与 JSON 两种格式 | 0 |
| `05-load-bad-binary.json` | 载入被翻转 1 字节的草图，CRC32 拦截 | 1（预期） |

样例响应是在本机**实际运行产物**，见 `samples/responses/*.response.json` 与 `*.plan.json`。
典型用法（也支持从 stdin 读请求）：

```bash
java -cp build/classes dev.dedup.hll.Main samples/requests/02-shard-merge.json
cat request.json | java -cp build/classes dev.dedup.hll.Main --compact --plan-out plan.json
```

## 7. 实测结果（如实记录，详见 [RESULTS.md](RESULTS.md)）

- 自动化测试：**4/4 测试类、266 657 项断言全部通过**，覆盖空集、重复插入幂等、
  4 分片合并不差 1 个寄存器、不兼容配置拒绝、9 类二进制坏格式与 8 类 JSON 坏格式。
- 固定哈希：Java 实现与独立 Python 移植版在 10 组 KAT 向量上 h1/h2 **逐向量一致**。
- 误差分布（p=12，每组 20 个固定种子试验，4 分片）：n=1000/100000/1000000 的
  样本标准差约 1.3%~1.6%，与理论 RSE 1.625% 吻合；**n=10000 这一组抽到波动偏大的种子集
  （std 3.13%）**，经独立 Python 版 HLL 复算确认是小样本抽样波动而非实现缺陷（详见 RESULTS.md）。
- 全部 4 分片合并试验中，合并草图与整体草图逐寄存器**完全一致（0 次不一致）**。
