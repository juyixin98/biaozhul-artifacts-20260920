# RUNLOG — 实际运行记录（如实）

环境：Linux 6.8.0-90-generic，Python 3.12.3，NumPy 2.5.3，pytest（`/home/admin/.local/bin/pytest`）。
以下命令均在项目根目录实际执行，输出为真实结果（对长 JSON 做了截取，数值未改动）。

## 1. 环境检查

```text
$ python3 --version && python3 -c "import numpy; print('numpy', numpy.__version__)"
Python 3.12.3
numpy 2.5.3
```

## 2. 测试：第一次运行 —— 9 个失败（真实记录，未通过项）

```text
$ python3 -m pytest -q
FAILED tests/test_group_isolation.py::test_group_isolation_with_random_row_blocks
FAILED tests/test_large_groups.py::test_many_equal_groups_hit_targets_closely
FAILED tests/test_service.py::test_split_endpoint_group_isolation_and_conservation
FAILED tests/test_service.py::test_split_endpoint_validates_ratios
FAILED tests/test_stratification.py::test_balanced_case_is_within_tolerance
FAILED tests/test_stratification.py::test_rare_class_cannot_appear_in_every_split
FAILED tests/test_stratification.py::test_two_split_rare_class_can_reach_both
FAILED tests/test_validation.py::test_duplicate_split_names_rejected
FAILED tests/test_validation.py::test_integer_group_ids_work
9 failed, 33 passed in 3.44s
```

根因：初版算法错误地强制「一个组只能有一个标签」，遇到组内多类别直接抛
`ValueError: Group ... contains conflicting labels`。真实分组数据（同一用户多条样本）
允许跨类别。**修复**：把每个组建模为「类别计数向量」，贪心目标改为最小化边际 L1 偏差；
同时把「组内冲突应报错」的测试改成「组内多类别应被支持且整组原子移动」。

## 3. 测试：重构后 —— 1 个失败 → 修复 → 全绿

```text
$ python3 -m pytest -q
FAILED tests/test_stratification.py::test_rare_class_cannot_appear_in_every_split
  assert any("'3'" in r ...)   # 原因串里整数标签的 repr 是 3 而非 '3'
1 failed, 41 passed in 2.88s
```

修正断言匹配文本后：

```text
$ python3 -m pytest -q
42 passed in 2.88s
```

（后续另把原因信息中 numpy 整数显示为 `np.int64(0)` 的展示瑕疵改为普通字符串。）

## 4. 端到端演示（默认合成数据，seed=42）

```text
$ python scripts/demo.py
数据集: n=2000, features=8, classes=3, groups=42
全局类别计数: {0: 3, 1: 1120, 2: 877}
组规模: min=3, max=500（g_00001 占 25%），mean≈47.6

切分大小: train=1401, val=301, test=298 （合计 2000，守恒）
各组数: train=16, val=9, test=17
stratified_within_tolerance = True
counts_within_tolerance     = True
max_class_proportion_deviation = 0.0015
max_split_count_deviation      = 0.001
rare_classes = ["0"]
reasons: 类别 0 仅出现在 1 个组，3 个切分中至少 2 个必然不含该类，
         不可避免偏差上限 0.0015（落在 0.05 容差内）

各类别比例偏差:
  0 global=0.0015  train=0.0021 val=0.0000 test=0.0000  maxdev=0.0015
  1 global=0.5600  train=0.5596 val=0.5615 test=0.5604  maxdev=0.0015
  2 global=0.4385  train=0.4383 val=0.4385 test=0.4396  maxdev=0.0011

独立校验 checks:
  total_samples_conserved=True, splits_pairwise_disjoint=True,
  groups_isolated_to_one_split=True, all_groups_assigned=True,
  reported_sizes_consistent=True, leaked_groups=[]

NumPy 逻辑回归（300 轮全批量梯度下降）:
  loss 1.0998 -> 0.0157
  accuracy: train=0.9971, val=0.9967, test=0.9765
  val/test 注明: 类别 0 因结构原因（组数 < 切分数）缺席
```

## 5. HTTP 服务实测

### 5.1 8000 端口被占用（环境问题，非代码问题）

```text
$ python3 -m grouped_splitter.service --port 8000
[1]+ Exit 1
OSError: [Errno 98] Address already in use
$ curl -s http://127.0.0.1:8000/health
{"status": "ok", "service": "pitjoin"}     # 机器上已存在的别的服务
```

改用 8731 端口后正常：

```text
$ python3 -m grouped_splitter.service --port 8731
$ curl -s http://127.0.0.1:8731/health
{"status": "ok", "service": "grouped-splitter"}
```

### 5.2 `/split`（examples/split_request.json，146 样本 / 37 组）

```text
split_sizes: {'train': 98, 'val': 24, 'test': 24}
class 0 global=0.3288  train=0.3265 val=0.3333 test=0.3333  maxdev=0.0046
class 1 global=0.3288  train=0.3265 val=0.3333 test=0.3333  maxdev=0.0046
class 2 global=0.3288  train=0.3265 val=0.3333 test=0.3333  maxdev=0.0046
class 3 global=0.0137  train=0.0204 val=0.0000 test=0.0000  maxdev=0.0137
stratified_within_tolerance=True, counts_within_tolerance=True
reasons: 类别 3 仅 1 个组、3 个切分 ⇒ 2 个切分必然不含该类；偏差 0.0137 在容差内
```

> 备注：最早的请求样例只有 20 行 / 9 组，test 仅能分到 1 个组，最大类别偏差 0.50
> （这是数据规模本身导致的真实结果，服务也如实返回了原因）。为更好展示机制，
> 已把样例扩充为 146 行 / 37 组的均衡数据 + 1 个稀有用户组。

### 5.3 `/demo`

```text
checks: 五个不变量全部 True
sizes: {'train': 352, 'val': 74, 'test': 74}
accuracy: train=1.0, val=1.0, test=1.0
```

### 5.4 参数校验（422）

```text
$ curl -X POST .../split -d '{"groups":["a","a","b"],"labels":[0,1,0],"ratios":[0.5,0.4]}'
HTTP 422
{"error": "Split ratios must sum to 1.0, got sum=0.9"}
```

## 6. 不可行场景：大组 + 稀有类（偏差与原因均返回）

构造 640 条样本：单个 `giant` 组 600 条（类 0），其余 40 个单样本组（类 1），70/30 两切分。

```text
split_sizes: {'split_0': 628, 'split_1': 12}
counts_within_tolerance: False
max_split_count_deviation: 0.2812
oversized_groups: ['giant']
reason: 类别 0 仅 1 个组、2 个切分 ⇒ 1 个切分必为 0，不可避免偏差上限 0.9375（超出容差）
reason: 切分大小偏差 0.2812 超过容差 0.05：组是原子的，最大组 'giant'(600/640=0.938)
        单独就使某切分偏移超过容差带
```

硬约束依旧满足：640 条全部守恒，`giant` 整组落在同一切分。

## 7. 可复现性实证

```text
same seed identical group assignment: True
shuffled rows identical group assignment: True
split sizes: {'train': 1401, 'val': 301, 'test': 298} sum: 2000
sample membership identical after un-permuting: True
```

## 8. 覆盖率

- 环境无 coverage 工具；尝试 `pip install pytest-cov` 被本会话权限策略拒绝
  （不允许在未经批准时下载外部包），**因此改用 Python 标准库 `trace` 在 pytest 运行期间度量**，未安装任何东西。
- 首次度量：总计 90.4%，但 `service.py` 仅 75.5%（低于 80% 单文件门槛）。
- 补充 8 个服务边界测试后出现 **1 个失败**：`/demo` 传字典 ratios 时切分名变成
  `split_0/split_1`（dict 被提前转成 tuple）。修复为透传 dict 后：

```text
$ python3 -m pytest -q
50 passed in 6.87s
```

最终行覆盖率（标准库 trace 实测）：

```text
__init__.py   100.0% (6/6)
model.py      100.0% (52/52)
models.py      97.6% (41/42)
pipeline.py    92.5% (74/80)
service.py     84.3% (86/102)
splitter.py    97.7% (129/132)
synthetic.py   86.4% (76/88)
TOTAL          92.4% (464/502)
```

未覆盖部分主要是服务端 500 兜底分支、`argparse` 入口与少量合成参数校验的防御性代码。

## 9. 未通过项 / 已知限制（如实）

- 开发过程中累计出现过 **9 + 1 + 1 = 11 个失败测试**，原因与修复见第 2、3、8 节；最终 50/50 通过。
- 未安装 `pytest-cov`（权限拒绝），覆盖率数字来自标准库 `trace`，口径为「可执行语句行」。
- 8000 端口被宿主机其他服务（`pitjoin`）占用，非本项目问题；实测使用 8731。
- 贪心分配在「大组 + 强不平衡」极端数据上给的是高质量近似最优，不保证全局最优；
  但硬约束（组隔离、守恒、可复现）在任何数据下都严格成立，超差时必带偏差数值与结构原因。
- 逻辑回归只用于验证切分产物能驱动训练循环，非精度导向；稀有类在 val/test 的缺席会被明确注明，不算模型失败。
