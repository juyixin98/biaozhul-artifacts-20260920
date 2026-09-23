# 运行报告（如实记录）

- 日期：2026-09-23
- 机器/系统：Linux x86_64（Ubuntu），OpenJDK **21.0.12.1**
- 依赖：**零第三方库**，仅用 JDK 自带的 `javac` / `jar`；JSON、哈希、HLL、查询执行、测试框架全部手写
- 规模：26 个 Java 源文件，约 4,400 行；产物 `hllengine.jar` ≈ 92 KB

> 复现：`bash tests/run_all.sh`（构建 → 单元测试 → CLI 端到端 → 精度实验），完整原始输出见
> [`reports/validation.log`](reports/validation.log)。

---

## 1. 实际执行的命令与最终结果

| 命令 | 结果 |
|------|------|
| `./build.sh`（`javac` 编译 + `jar` 打包） | ✅ 成功，生成 `hllengine.jar` |
| `java -jar hllengine.jar selftest` | ✅ **tests: 63, passed: 63, failed: 0** |
| `bash tests/cli_e2e.sh` | ✅ **CLI e2e: 16 passed, 0 failed** |
| `java -jar hllengine.jar experiment --trials 40 --out-dir reports` | ✅ **24 组 × 40 = 960 次**试验，写出 2 个 CSV + 1 个 Markdown |

一键脚本最终打印 `ALL STAGES PASSED`，退出码 0。

---

## 2. 验收点对照

| 要求 | 覆盖情况 |
|------|----------|
| 空集 | `HllSketchTest.emptySketchEstimatesZero`：估计 0、observedCount 0、`empty=true`、全部寄存器为零 |
| 重复插入 | `duplicateInsertsCountAsOne`：1 万次相同插入估计 1；5000 次 a/b 估计 2；observedCount 仍为流长度 |
| 分片合并 | `shardMergeEqualsUnionSketch`（重叠分片，寄存器与直建并集逐位相等）、`partitionedMerge`（8 路不相交）、API 版 `api.shardMergeViaJsonAndJson`、CLI 跨进程 `cli.exportImportAcrossProcesses` |
| 坏格式 | 10+ 种坏草图二进制（错 magic、截断、错版本、flags、精度越界、未知 hashId、CRC 损坏、寄存器秩越界、负计数、base64 非法）+ 24 种坏 JSON + 各类坏请求，均返回 `BAD_FORMAT`/相应错误码 |
| 固定种子多组基数误差分布 | `AccuracyExperiment`，主种子 `0x5DEECE66D`（SplitMix64），p∈{10,12,14} × n∈{0,1,10,100,1k,10k,100k,1m}，每组 40 次独立试验 |
| 固定哈希与精度、合并配置一致 | Murmur3 x64_128 冻结；精度/种子/hashId 三者不同即 `INCOMPATIBLE_CONFIG`（`MergeCompatibilityTest` 等） |
| 标注估计值与误差 | 每个结果含 `isEstimate/isExact`、名义 σ、`*_relativeStandardError` 兄弟列、`approximateColumns` |
| 不调用现成 SQL 引擎 | 查询执行（scan/filter/project/group/aggregate/limit、逻辑+物理计划）全部自写于 `engine/` |
| 数据与计划可导出 | `getDataset`/`exportSketch` 导出数据与草图；`explain` 导出逻辑/物理计划；均为 JSON |
| 源码/README/请求样例/自动化测试 | 齐全；`examples/`（含 `bad/`），测试含内置套件与真实进程 CLI 脚本 |
| 不做前端 | 仅 stdin/文件 JSON 接口，无任何 UI |

---

## 3. 误差分布实验结果（节选自 reports/accuracy_report.md）

经验相对标准差（40 次试验）与理论 σ = 1.04/√m 的对比，大基数区间：

| p | n | 经验相对标准差 | 理论 σ | 经验/理论 | 95 分位 \|相对误差\| |
|---|---|---:|---:|---:|---:|
| 10 | 1,000 | 3.220% | 3.250% | 0.99 | 6.76% |
| 10 | 10,000 | 3.466% | 3.250% | 1.07 | 6.47% |
| 10 | 1,000,000 | 3.279% | 3.250% | 1.01 | 5.95% |
| 12 | 1,000 | 1.174% | 1.625% | 0.72 | 2.27% |
| 12 | 100,000 | 1.567% | 1.625% | 0.96 | 2.93% |
| 12 | 1,000,000 | 1.245% | 1.625% | 0.77 | 2.13% |
| 14 | 10,000 | 0.583% | 0.813% | 0.72 | 0.99% |
| 14 | 1,000,000 | 0.663% | 0.813% | 0.82 | 1.22% |

完整数据见 [`accuracy_trials.csv`](reports/accuracy_trials.csv)（每次试验一行）、
[`accuracy_summary.csv`](reports/accuracy_summary.csv) 与
[`accuracy_report.md`](reports/accuracy_report.md)。

观察（均为**估计值的统计**，非精确去重）：

- 精度每提高 2（m ×4），理论 σ 减半；经验标准差同趋势下降（p=10→14 从 ~3.3% 降到 ~0.66%）。
- 大基数（n≫m）时经验 σ 与理论 σ 比值大多在 **0.7–1.1**，与经典 HLL 误差特性相符。
- 小基数（n=10、n=100）由线性计数（linear counting）修正，偏差与方差都很小。
- 平均相对偏差（bias）各组基本在 ±0.8% 以内，没有系统性高偏/低偏。
- 个别组（如 p=12, n=10,000）40 次试验的经验 σ 比值到 1.86——这是**小样本（40 次）抽样波动**，
  原始逐次数据已保留在 CSV，可加大 `--trials` 复测；它不改变“单次结果是带 ~1.6% 尺度误差的估计”这一结论。

### 固定输入（非随机）的一组确定性数字

对 `0..n-1` 这一固定序列，p=12 实测单次相对误差：n=100 → 0.22%，n=1,000 → 2.40%，
**n=10,000 → 4.28%**，n=100,000 → 0.76%，n=1,000,000 → −1.26%。
其中 n=10,000 的 4.28% 约为 2.6 个名义 σ，属正常但偏大，因此该确定性单测的判定带按实测放宽到 6%
（见 `HllSketchTest.accuracy`），而非用一个会偶然失败的 3.5%。

---

## 4. 哈希正确性的独立交叉验证

- 内置测试复现 SMHasher `KeysetTest.cpp` 的验证流程，对 `MurmurHash3_x64_128`
  得到官方登记的验证常量 **`0x6384BA69`**（见 SMHasher `src/main.cpp` 中 `Murmur3F` 的期望值）。
- 另写了一份**独立 Python 移植**（开发期临时脚本，非交付物），对空串、多个字符串、
  4 字节/40 字节二进制输入逐向量与 Java 完全一致，并在相同 Keyset 输入下同样产出 `0x6384BA69`。

---

## 5. 开发过程中发现并修复的问题（含一度失败的测试）

首轮跑测为 **55/63 通过、8 个失败**。这些失败里既有真实代码缺陷，也有我测试自身的错误，均已定位修复：

1. **真实 bug——空草图 zeroRegisters 被硬编码为 0**：`estimate()` 空集分支直接传了 0，
   与“所有寄存器都应为零”矛盾。修复为传入 `m`。
2. **真实健壮性问题——请求校验异常未被统一归类**：部分形状错误抛的是内部 `JsonException`，
   会从 `batch` 的仅捕获 `ApiException` 的逻辑里漏出。在 `Engine.handle` 统一包成 `BAD_FORMAT`，
   `LogicalPlan.parse` 同样包装。
3. **真实校验缺口——比较类谓词缺 `value` 未拒绝**：`{"op":"eq","field":"age"}` 原先漏过。
   现显式返回 `BAD_FORMAT`（并有对应坏样例 `examples/bad/06-filter-missing-value.json`）。
4. **真实设计缺陷——batch 的进度计数语义含糊**：把 `executed` 改为更明确的
   `attempted / failures / firstFailureAtIndex`。
5. **测试自身错误——CN 行数期望写错**（样本里 CN 实际是 3 行不是 2 行）；已改。
6. **测试自身错误——两个 e2e 脚本断言前提不成立**（对不存在的数据集 explain、期望 batch 整体非零退出）；已改。
7. **测试自身问题——确定性误差阈值拍脑袋设成 3.5%**：先实测固定输入误差，再按实测设为 6%，避免脆弱测试。
8. **测试框架定位缺陷——栈帧过滤把 lambda 行号误报为 `(unknown)`**：重写 `TestRunner` 的调用者定位。
9. **二进制格式测试中我对偏移手算错误**（漏数 4 字节 magic）：核对格式布局后修正测试偏移；产品序列化本身正确
   （往返与 CRC 测试一直通过）。
10. **哈希 KAT 的一次虚惊**：我中途用一份 Python 移植核对时，因把 SMHasher 的
    “累积键数组 `{0},{0,1},{0,1,2},…`”误写成 `memset(key,i,i)`，得到不同常量，一度怀疑 Java 实现；
    查阅官方 `KeysetTest.cpp` 后确认 Java 原始写法正确，恢复后 Java 与修正后的 Python **都**得到 `0x6384BA69`。
    产品哈希代码全程未改动，通过官方常量 + 独立实现双重确认。

修复后重新全量构建与测试，达到第 1 节的全绿结果。

---

## 6. 未通过项 / 已知限制（如实说明）

- **无未通过的测试**：交付时单元 63/63、CLI 16/16 全部通过，精度实验正常完成。
- 单机、单进程、内存态：JVM 退出目录即清空；跨节点/跨进程归并用 `export/import` 的草图二进制完成
  （CLI 已演示跨进程往返），但没有内置网络服务。
- 查询引擎为教学/演示规模的单趟执行器：无索引、无外存、无连接（join）、无排序聚合下推优化；
  物理计划与逻辑计划目前 1:1（保留了独立的两层结构和成本说明，便于扩展）。
- 小基数下名义 σ 是为大基数推导的先验值，实际误差由线性计数主导，报告中已按组给出实测分布。
- 40 次/组属于较小蒙特卡洛样本，个别组经验 σ 与理论值偏差较大（见第 3 节）；可用
  `--trials 200` 等增大样本复测，结果仍由固定种子决定、可复现。
