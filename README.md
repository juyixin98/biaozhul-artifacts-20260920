# 分组分层数据切分服务（Group-aware Stratified Splitter）

纯后端、本地运行的机器学习基础设施服务：在 **同一组（group）样本不得跨切分集合** 的硬约束下，
尽量保持各切分集（train/val/test…）的类别比例；当两者不可同时满足时，返回实测偏差与结构性原因。
不下载任何外部模型或数据，全部用可复现的合成数据和 NumPy 实现的简单逻辑回归验证核心机制。无前端。

## 1. 环境

- Python 3.12（3.10+ 即可）
- 依赖：`numpy>=1.24`（仅此一个运行时依赖）
- 测试：`pytest`（环境中已具备；覆盖率用标准库 `trace` 度量，无需安装额外包）

```bash
pip install -r requirements.txt
```

## 2. 目录结构

```text
grouped_splitter/
  splitter.py     # 核心算法：组聚合 + 贪心最小化类别/数量边际 L1 偏差
  models.py       # 不可变结果对象（SplitResult / SplitDiagnostics / ...）
  synthetic.py    # 可复现合成数据（含大组、稀有类别、组内偏移、线性可分信号）
  model.py        # NumPy 多项逻辑回归（softmax + 全批量梯度下降）
  pipeline.py     # 端到端：合成数据 -> 切分 -> 独立校验 -> 训练评估
  service.py      # 纯标准库 HTTP JSON 服务（GET /health, POST /split, POST /demo）
scripts/demo.py   # 命令行演示，打印 JSON 报告
examples/         # 请求与响应样例
tests/            # 50 个 pytest 用例
```

## 3. 快速开始

### 3.1 命令行演示（离线，秒级）

```bash
python scripts/demo.py                      # 默认 2000 样本 / 40 组 / seed=42
python scripts/demo.py --samples 500 --groups 20 --seed 7
python scripts/demo.py --no-model           # 只切分与校验，不训练
```

报告含：数据集概况、各切分集大小与类别比例、最大类别比例偏差、切分数量偏差、
不可行原因、五条不变量校验结果、逻辑回归在各集合上的指标。

### 3.2 HTTP 服务

```bash
python -m grouped_splitter.service --host 127.0.0.1 --port 8731
```

> 说明：本机 8000 端口已被其他服务占用（实测返回的 `service` 名是 `pitjoin`），
> 因此文档与实测使用 8731；端口可通过 `--port` 任意指定。

```bash
curl -s http://127.0.0.1:8731/health

curl -s -X POST http://127.0.0.1:8731/split \
  -H 'Content-Type: application/json' \
  -d @examples/split_request.json

curl -s -X POST http://127.0.0.1:8731/demo \
  -H 'Content-Type: application/json' \
  -d @examples/demo_request.json
```

请求/响应样例：`examples/split_request.json`、`examples/split_response_example.json`、
`examples/demo_request.json`。

## 4. HTTP API

### `GET /health`
返回 `{"status":"ok", ...}`。

### `POST /split`
请求字段：

| 字段 | 类型 | 说明 |
|------|------|------|
| `groups` | array，必填 | 每个样本的组 id（字符串/整数等可哈希值） |
| `labels` | array，必填 | 每个样本的类别标签；一个组允许混合多个类别 |
| `ratios` | array 或 object | 默认 `[0.7, 0.15, 0.15]`；用对象可自定义切分名，如 `{"train":0.8,"test":0.2}` |
| `split_names` | array | ratios 为数组时自定义名字 |
| `seed` | int | 默认 42，固定种子 |
| `tolerance` | float | 默认 0.05，仅用于合格判定，不改变分配结果 |
| `include_assignments` | bool | 默认 true，是否返回逐样本索引 |

错误码：请求体问题返回 400；核心参数校验失败（比例不为 1 等）返回 422；未知路径 404。

### `POST /demo`
离线跑完整合成管线。字段：`n_samples,n_features,n_classes,n_groups,ratios,seed,tolerance`。

## 5. 算法

1. **组聚合（硬约束）**：按 `group_id` 把样本聚合成不可分割的原子单元，每个组携带一个
   类别计数向量（支持组内多类别）。整个组只能进入一个切分集，因此同组绝不跨集。
2. **规范化排序**：组按（规模降序、组 id）做内容相关的确定性排序；输入行的任何重排都不影响结果。
3. **贪心分配**：从大组开始，逐个把组放入「放入后类别计数与目标 `ratio×各类别总数` 的
   边际 L1 偏差增量」最小的切分集，并以 0.5 权重兼顾切分总数量目标；同分时用种子随机微扰打破平局。
4. **偏差与原因**：分配后独立计算
   - `max_class_proportion_deviation`：各切分集类别比例相对全局比例的最大绝对偏差；
   - `max_split_count_deviation`：切分样本数相对目标的最大偏差（按 n 归一化）；
   - 结构性原因，例如：
     - 某类别出现的组数少于切分数 ⇒ 至少有切分集必然不含该类（稀有类别）；
     - 单个原子组规模超过容差带 ⇒ 切分大小必然超差（大组）；
     - 组数少于切分数 ⇒ 必有空切分集。

**可复现性**：合成数据用 `np.random.default_rng(seed)` 生成；切分结果只依赖数据内容与种子。
注意：种子只在同分情况下影响平局，改变种子不会破坏任何硬约束。

## 6. 验收点对照

| 验收要求 | 覆盖位置 |
|----------|----------|
| 大组 | `tests/test_large_groups.py`（60% 巨型组、合成数据 25% 大组） |
| 稀有类别 | `tests/test_stratification.py`（单组稀有类、原因文本） |
| 输入重排不变 | `tests/test_determinism.py::test_input_row_permutation_invariance`、`test_repeated_shuffles_stable` |
| 组隔离 | `tests/test_group_isolation.py`、`pipeline.verify_invariants` 独立复核 |
| 总数守恒 | 分区为 `0..n-1` 的无重无漏划分（多处断言） |
| 固定种子稳定 | `test_same_seed_same_result`、`test_synthetic_generator_is_reproducible` |
| 偏差 + 原因 | 巨型组场景实测 `max_split_count_deviation=0.2812` 且返回命名大组的原因 |

## 7. 测试

```bash
python -m pytest -q
```

实测：**50 passed**。

用标准库 `trace` 度量的行覆盖率（无需联网安装）：

| 文件 | 覆盖率 |
|------|--------|
| `splitter.py` | 97.7% |
| `pipeline.py` | 92.5% |
| `service.py` | 84.3% |
| `synthetic.py` | 86.4% |
| `model.py` | 100.0% |
| `models.py` | 97.6% |
| **总计** | **92.4%（464/502）** |

## 8. 实际运行记录

完整命令、真实输出、开发中出现过的失败项与修复，见 **[RUNLOG.md](RUNLOG.md)**。
该文件如实记录了初版测试的 9 个失败、设计修正（支持组内多类别）以及最终通过情况。

## 9. 范围说明

- 纯后端，无任何前端代码。
- 不访问网络、不下载模型或数据；HTTP 服务只监听本地，唯一第三方运行依赖是 NumPy。
- 逻辑回归仅用于验证切分产物可直接驱动真实训练循环，并非追求模型精度。
