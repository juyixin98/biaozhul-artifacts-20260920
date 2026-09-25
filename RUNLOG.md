# 运行记录（RUNLOG）

本文件如实记录开发完成后的实际运行命令与结果。环境：

* OS：Linux 6.8.0-90-generic (x86_64)
* Python：3.12.3
* NumPy：2.5.3
* pytest：9.1.1
* 日期：2026-09-24

所有命令均在仓库根目录执行。

## 1. 自动化测试

命令：

```bash
python3 -m pytest -q
```

结果（最近一次完整运行）：

```
107 passed in 1.1s
```

无失败、无跳过。测试分布：

| 文件 | 覆盖内容 |
| --- | --- |
| `tests/test_basic.py` | 菱形图 Pareto、路径重建权重/节点校验、s==t、不可达、JSON 可序列化 |
| `tests/test_equal_and_zero_cycle.py` | 相等标签共存、平行边去重、零权环终止、浮点容差、同顶点集不同排列回归 |
| `tests/test_budget_and_limits.py` | 单/双维预算、边界容差、标签截断、无向图 |
| `tests/test_validation.py` | 缺字段、负权、NaN/Inf、布尔 ID、越界权重、eps/label_cap 范围等失败状态 |
| `tests/test_reference_agreement.py` | 随机 DAG/含环图/预算实例，独立 DFS 枚举对照；直接比较路径顶点序列集合 |
| `tests/test_cli.py` | stdin/文件输入、退出码、坏 JSON |

## 2. 随机交叉验证（临时脚本，结论保留在此）

用与求解器完全独立的 DFS 枚举所有简单路径做对照（脚本未入库，逻辑等价于
`tests/test_reference_agreement.py` 中的参数化用例的加强版）：

* 400 个随机含环图（3–7 顶点，含大量零权边，部分带预算）：**Pareto 路径顶点
  序列集合 400/400 完全一致**；
* 200 个随机 DAG（4–8 顶点，带预算）：**200/200 完全一致**；
* 另有 300 例按权重多重集核对：300/300 一致。

## 3. 命令行 / 库接口

退出码实测：

| 场景 | 命令 | 退出码 |
| --- | --- | --- |
| ok | `python -m mospp examples/01_basic.json` | 0 |
| unreachable | `python -m mospp examples/06_unreachable.json` | 0 |
| truncated | `python -m mospp examples/05_truncated.json` | 0 |
| invalid_request | `python -m mospp examples/07_invalid.json` | 1（响应在 stdout） |
| 输入是坏 JSON | `echo '{bad' \| python -m mospp` | 1（错误信息在 stderr） |

库调用 `solve_request()` 对 README 示例返回：

```
status: ok
pareto: [{'time': 2.0, 'cost': 5.0}, {'time': 5.0, 'cost': 0.0}]
```

## 4. 样例输出

`examples/output/*.json` 为下列命令的实际 stdout（已随仓库提交）：

```bash
for f in examples/0*.json; do
  python -m mospp "$f" > "examples/output/$(basename "$f")"
done
```

各样例状态：

| 样例 | status | 说明 |
| --- | --- | --- |
| 01_basic | ok | Pareto = (2,5)、(5,0) |
| 02_budgets | ok | 时间≤4 且费用≤6，只剩 (2,5) |
| 03_equal_labels | ok | 两条同权 (2,2) 的不同路径均保留 |
| 04_zero_cycle | ok | 零权环被跳过（cycle_skipped=1），单 Pareto 点 (6,4) |
| 05_truncated | truncated | label_cap=3，自报 truncated=true |
| 06_unreachable | unreachable | 源点不可达终点 |
| 07_invalid | invalid_request | 负权被拒，退出码 1 |
| 08_undirected | ok | 无向图 |

## 5. 性能抽查

分层 DAG（180 顶点、730 弧、15 层×12）：

```
status=ok  pareto=30  labels_created=6470  耗时≈0.15s
```

输出经校验两两非支配。规模/标签上限的硬边界见 README“输入范围与限制”。

## 6. 开发过程中发现并修复的真实问题（如实记录）

以下都是在“随机枚举对照”环节被测试实际打出来、而非凭空假设的问题：

1. **测试夹具的顶点下标错位**：辅助建图函数按“边出现顺序”隐式分配内部下标，
   当 ID 不连续（如 0,1,2,4,5,3）时，测试用数值当内部下标导致路径重建错乱。
   修复为按 ID 排序预注册全部顶点。这是测试侧问题，库的 JSON 路径经
   `from_request` 校验始终正确。
2. **终点标签缺少纯权重收尾过滤**：目标点不扩展，过程中两个 visited 互不包含
   的标签无法互相淘汰，到终点可能残留严格被权重支配的点。增加 `_terminal_filter`。
3. **严格支配误加 visited 子集条件**：初版把“严格权重支配”与 visited 子集绑定，
   导致经典菱形图里 (2,5) 无法淘汰前缀不同的 (4,5)。修正为：严格权重支配与
   visited 无关（同顶点出边集相同，非负权下延伸保持支配）。
4. **同顶点集不同排列被错误合并（最关键）**：两条路径 0-2-1-3-5 与 0-2-3-1-5
   访问顶点集相同、顺序不同，初版仅凭 visited 相等判重，漏掉一条。最终把
   “路径身份”定义为 **顶点序列**（而非顶点集合或 Arc 对象身份），并用
   `Label.same_path()` 沿前驱链逐跳比较；同顶点集不同排列正确共存，平行边
   （端点相同）按顶点序列去重。该回归已固化为
   `test_same_vertex_set_different_permutations_kept` 与
   `test_cyclic_path_sets_match_enumeration`。
5. **容差边界把 2e-16 舍入差误判为支配**：累计值 1.0000000000000002 与 1.0 的
   比较改成对称三分类（两方向都无带外严格维即视为相等），避免伪支配。

修复后 600+ 随机实例的路径集合与独立枚举全部一致。

## 7. 未通过项 / 已知限制

* 截至本次记录，**全部 107 个入库测试通过，无未通过项**。
* 设计上的边界（非缺陷）：仅接受非负权重；输出限定简单路径；每顶点标签集为
  O(k) 线性比较；Pareto 标签数可能随图结构指数增长，由 `label_cap` +
  `truncated` 显式兜底。
