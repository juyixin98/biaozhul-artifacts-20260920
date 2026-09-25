# group-split：分组分层数据切分服务

纯后端本地机器学习基础设施组件。给定逐样本的 **组 ID** 与 **类别标签**，把数据集切分为
train/val/test 等命名集合，满足：

- **组隔离（硬约束）**：同一组的所有样本必须落在同一个集合，绝不拆分；
- **总数守恒（硬约束）**：每个样本恰好被分配到一个集合；
- **类别比例保持（软目标）**：在组隔离约束下，各集合的类别分布尽量贴近请求比例；
- **冲突时如实报告**：当组隔离与比例保持不可兼得时，返回量化偏差与结构化原因
  （`LARGE_GROUP` / `RARE_CLASS` / `RESIDUAL_IMBALANCE`）。

不下载任何外部模型或数据；验证全部基于可复现的合成数据（`numpy.random.Generator`）。

## 核心机制

`group_split/splitter.py`：

1. 按组聚合类别计数，得到每组的类别向量；
2. 计算各集合的目标计数：`ratio[s] × 全量类别计数`（含集合大小本身）；
3. 以 `(-组大小, BLAKE2b(seed, 组ID))` 确定性排序——**排序键只依赖组 ID 与种子，
   不依赖输入行顺序**，因此输入重排结果不变；BLAKE2b 替代内置 `hash()`，
   结果与 `PYTHONHASHSEED` 无关；
4. 贪心分配：每组放入使 L1 偏差增量最小的集合，平局用 `BLAKE2b(seed, 组ID, 集合名)`
   打破；
5. 计算偏差报告（每集合每类别的相对偏差 `(实际-目标)/max(目标,1)`），超容差时
   附诊断原因：
   - `LARGE_GROUP`：组大小超过最小集合的目标容量，组隔离禁止拆分；
   - `RARE_CLASS`：某类别所在组数少于集合数，必有集合缺失该类别；
   - `RESIDUAL_IMBALANCE`：以上都不成立时的粒度残余偏差。

## 安装与运行

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt   # numpy + pytest
```

### 库调用

```python
from group_split import split_groups

result = split_groups(
    groups=["g1", "g1", "g2", "g2"],
    labels=[0, 1, 0, 0],
    ratios={"train": 0.5, "test": 0.5},
    seed=42,
)
result.assignment      # {'g1': 'test', 'g2': 'train'}（示例）
result.split_indices   # {'train': array([...]), 'test': array([...])}
result.report          # 偏差报告（含 reasons）
```

### 本地 HTTP 服务（仅标准库）

```bash
.venv/bin/python -m group_split.service --host 127.0.0.1 --port 8080
```

- `GET /health` → `{"status": "ok"}`
- `POST /split`，请求体见 [`examples/split_request.json`](examples/split_request.json)：

```json
{
  "groups": ["g1", "g1", "g2", "g2", "..."],
  "labels": [0, 1, 0, 0, "..."],
  "splits": {"train": 0.6, "val": 0.2, "test": 0.2},
  "seed": 42,
  "tolerance": 0.05,
  "include_sample_splits": true
}
```

响应：`assignment`（组→集合）、`split_sizes`、`report`（偏差与原因），
可选 `sample_splits`（逐样本集合名）。校验失败返回 `400 {"error": ...}`。

```bash
curl -s -X POST http://127.0.0.1:8080/split \
  -H 'Content-Type: application/json' \
  --data @examples/split_request.json | python3 -m json.tool
# 或：.venv/bin/python examples/client.py --port 8080
```

### 演示脚本

```bash
.venv/bin/python examples/run_demo.py
```

## 验收结果实录

以下均为本机实际运行输出（Linux，Python 3.12.3，numpy 2.5.3，pytest 9.1.1）。

### 1. 自动化测试

```bash
$ .venv/bin/python -m pytest -q
.....................                                                    [100%]
21 passed in 3.33s
```

**21 passed, 0 failed。** 覆盖：

| 验收点 | 测试 |
|---|---|
| 组隔离 | `test_group_isolation_and_conservation_{balanced,large_group,rare_class}` |
| 总数守恒 | 同上（索引不重不漏，恰好覆盖 `0..N-1`） |
| 固定种子稳定性 | `test_fixed_seed_is_stable`、`test_split_is_deterministic_over_http` |
| 输入重排不变性 | `test_input_reordering_is_invariant` |
| 大组冲突与原因 | `test_large_group_forces_deviation_and_reason` |
| 稀有类别冲突与原因 | `test_rare_class_forces_deviation_and_reason` |
| 平衡数据分层质量 | `test_stratification_within_tolerance_on_balanced_data` |
| 输入校验 | 长度不符 / 空数据 / 比例和≠1 / 非正比例 / 非正容差 |
| HTTP 服务 | 健康检查、往返、确定性、可选逐样本输出、400/404 |

### 2. 三种场景演示（`examples/run_demo.py`，seed=42）

```
=== balanced ===
samples=10480 groups=300 within_tolerance=True max_deviation=0.0337
  train size= 7340 target=  7336.0 class_counts={'0': 5163, '1': 1450, '2': 727}
  val   size= 1568 target=  1572.0 class_counts={'0': 1114, '1': 298, '2': 156}
  test  size= 1572 target=  1572.0 class_counts={'0': 1107, '1': 308, '2': 157}

=== large group (45% of samples) ===
samples=2342 groups=100 within_tolerance=False max_deviation=0.0661
  ...
  reason[LARGE_GROUP]: group 99 has 1054 samples, exceeding the smallest
  split's target size 351.3; group isolation forbids dividing it

=== rare class (4 samples in 1 group) ===
samples=5049 groups=200 within_tolerance=False max_deviation=0.6000
  ...
  reason[RARE_CLASS]: class 'rare' appears in only 1 group(s) (4 samples),
  fewer than the 3 splits; split(s) ['val', 'test'] contain no sample of it

input reordering invariance: OK
```

### 3. HTTP 服务实测

```bash
$ .venv/bin/python -m group_split.service --port 8123 &
$ curl -s http://127.0.0.1:8123/health
{"status": "ok"}
$ curl -s -X POST .../split --data @examples/split_request.json
{"assignment": {"g1": "test", "g2": "train", ...},
 "split_sizes": {"train": 12, "val": 4, "test": 4},
 "report": {"within_tolerance": false, "max_deviation": 0.1667, ...}}
$ curl -s -o /dev/null -w "%{http_code}" -X POST .../split -d '{"groups":[1],"labels":[0]}'
400
```

注：20 样本 / 10 组的微型样例在 0.05 容差下 `within_tolerance=false` 属预期行为
（组粒度太粗），报告如实给出偏差 0.1667。

### 4. 跨进程稳定性

```bash
$ PYTHONHASHSEED=1  .venv/bin/python -c '...split...'  > run1.txt
$ PYTHONHASHSEED=99 .venv/bin/python -c '...split...'  > run2.txt
$ diff run1.txt run2.txt && echo OK
PYTHONHASHSEED-independent: OK
```

## 已知限制（如实说明）

- 贪心算法不保证全局最优的类别比例；平衡合成数据上实测最大相对偏差 0.0337
  （容差 0.05），更极端的组大小分布会更差，报告会如实反映。
- 大组场景（45% 样本集中于 1 组）下偏差 0.0661 略超 0.05 容差——这是组隔离
  硬约束的必然代价，服务返回偏差值与 `LARGE_GROUP` 原因而非静默失败。
- 稀有类别（4 样本 / 1 组）必然缺席部分集合（`max_deviation=0.6`），
  由 `RARE_CLASS` 原因说明。
- 未通过项：无（21/21 通过）。

## 项目结构

```
group_split/
  splitter.py    # 核心算法与偏差报告
  synthetic.py   # 可复现合成数据（平衡 / 大组 / 稀有类别）
  service.py     # 本地 HTTP JSON 服务（仅标准库）
examples/
  split_request.json  # 请求样例
  client.py           # 样例客户端
  run_demo.py         # 三场景演示
tests/
  test_splitter.py    # 核心验收测试
  test_service.py     # 服务测试
```
