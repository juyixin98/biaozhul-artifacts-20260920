# 运行记录（实际执行，未删减失败项）

记录环境、实际执行的命令与结果。开发过程中遇到的失败也如实保留在文末。

## 环境

| 项 | 实测值 |
|---|---|
| 操作系统 | Linux 6.8.0-90-generic |
| Python | 3.12.3 |
| NumPy | 2.5.3 |
| pytest | 9.1.1 |
| pytest-cov | 已安装（随环境） |
| 硬件/ROS | 无、不依赖；全部为合成数据 |

依赖安装（如需要）：

```bash
pip install -r requirements.txt        # numpy>=1.24
# 测试需要: pip install pytest pytest-cov
```

## 1. 自动化测试

命令：

```bash
python3 -m pytest tests/ -q --cov=pose_graph --cov-report=term
```

结果（最近一次完整运行）：

```
..............................................................           [100%]
63 passed in 1.64s

Name                      Stmts   Miss  Cover
---------------------------------------------
pose_graph/__init__.py        4      0   100%
pose_graph/cli.py            23      1    96%
pose_graph/graph.py         123      7    94%
pose_graph/json_io.py        73      2    97%
pose_graph/optimizer.py     175     11    94%
pose_graph/residual.py       25      0   100%
pose_graph/robust.py         33      2    94%
pose_graph/se2.py            24      0   100%
pose_graph/synthetic.py      84      0   100%
---------------------------------------------
TOTAL                       564     23    96%
```

63 个用例全部通过，行覆盖率 96%（满足 ≥80% 规范）。未覆盖的 23 行主要是
固定节点前提下不可达的防御分支（奇异矩阵重试、阻尼加到上限、部分输入校验）。

## 2. JSON 命令行样例（实际输出）

```bash
for f in examples/*.json; do
  python -m pose_graph.cli "$f" \
    -o "examples/responses/$(basename ${f%.json}).response.json"
done
```

```
status=ok iterations=5 -> examples/responses/minimal.response.json
status=ok iterations=7 -> examples/responses/square_loop.response.json
status=ok iterations=7 -> examples/responses/bad_loop_closure_linear.response.json
status=ok iterations=7 -> examples/responses/bad_loop_closure_geman_mcclure.response.json
status=ok iterations=7 -> examples/responses/disconnected_two_components.response.json
```

响应摘要实测：

| 样例 | cost 初始→最终 | 收敛 | 固定 | 自动锚定 | 连通 |
|---|---|---|---|---|---|
| square_loop | 10.264 → 0.027 | True | [0] | [] | True |
| bad_loop_closure_linear | 151.534 → 7.860 | True | [0] | [] | True |
| bad_loop_closure_geman_mcclure | 11.257 → 1.022 | True | [0] | [] | True |
| disconnected_two_components | 2.369 → 0.014 | True | [0] | [16] | False |

错误输入退出码实测：非法 JSON / 结构错误返回退出码 2（见
`tests/test_cli_and_validation.py` 与 `tests/test_json_io.py`）。

## 3. 验收场景实测数值

### 3.1 合成闭环 + 残差下降

- cost：10.2635 → 0.02690，7 次迭代，`relative_cost_change_below_ftol` 收敛；
- cost_history 全程单调不增；
- 对真值最大平移误差 0.286 m（边长 10 m），wrap 后最大航向误差 0.0499 rad；
- 固定节点 0 全程严格保持 `(0, 0, 0)`。

### 3.2 错误回环 + 鲁棒核对比（注入 3 m / 0.4 rad 错误约束，初始 s≈141.3）

| 核 | cost 初始→最终 | 轨迹最大平移偏差 | 错误边最终 s |
|---|---|---|---|
| linear | 151.5 → 7.86 | 3.526 m | 1.50 |
| huber(k=1) | 33.0 → 7.75 | 3.320 m | 2.20 |
| cauchy(k=1) | 15.2 → 4.89 | 1.083 m | 86.43 |
| geman_mcclure(k=1) | 11.3 → 1.02 | 0.290 m | 184.42 |

补充实测：huber 在 k=0.5/0.3/0.2 时轨迹偏差分别为 2.63/2.00/1.57 m，
仍无法彻底拒绝该极端外点（非重降核的固有性质，README 已如实说明）。

### 3.3 跨 ±π 角度

- 角度切口小图：cost 21.215 → 0，4 次迭代；最大角度残差 0.233 → 0；
- 15 段累计 6.75 rad（>2π）长链：cost 0.2213 → 0，逐边角度残差 → 0
  （终态绝对 θ=0.4668 与 6.75 模 2π 等价）。

### 3.4 不连通诊断

- 双环图：`is_connected=False`，2 个分量；无锚分量自动锚定节点 16；
- `strict_anchoring=True`：抛出
  `GraphStructureError: strict_anchoring: 1 component(s) without a fixed node`。

## 4. 开发过程中出现过的失败（已修复，保留以如实记录）

1. **编辑引入缩进错误**：一次手工编辑把 `if step_norm < xtol:` 与下一行
   合并，导致 `IndentationError: unexpected indent`（optimizer.py:231）。
   冒烟测试立即暴露，已修正。
2. **角度区间约定自相矛盾**：初版文档写 `(-pi, pi]`，实现
   `(a+pi) % 2pi - pi` 实际给出 `[-pi, pi)`（+3π 被映射到 −π）。
   首个测试版本按错误文档断言而失败。处理方式：统一约定为 `[-pi, pi)`
   （两端是同一物理角），同步修正文档与测试，而非改动数学实现。
3. **对 Huber 的过度断言**：初版测试断言三种鲁棒核都能把轨迹偏差压到
   <1.2 m，Huber 实测为 3.32 m 而失败。核实这是 Huber 非重降性质
   （权重 k/√s 不为 0）所致，不是实现 bug。改为：拒绝性断言只针对
   Cauchy/Geman–McClure，并新增一条测试与 README 段落如实记录 Huber
   只能限幅、不能彻底拒绝 gross outlier。
4. **JSON 根为数组时 500 式失败**：`run_from_dict` 先解析 options，对 list
   调用 `.get` 抛 `AttributeError`。已调整为先做根对象/图结构校验，
   现在对任何畸形输入统一返回 `status:"error"`。
5. **压力脚本断言方向写反**：首次多种子压测把单调性写成了"非递减"，
   150 例全部"失败"；核对后确认是脚本本身断言反向（库代码无此问题），
   改正后 150 例全部通过。

## 5. 多随机种子压力测试

```bash
# 50 个种子 × 3 种噪声（0.01/0.1/0.3），每 3 个种子注入 1 条 cauchy 鲁棒坏边
```

150 个随机合成图全部满足：cost 历史单调不增（容差 1e-9）、最终 cost 不大于
初始、所有位姿为有限值，**0 失败**。

## 6. 未做的事项（边界声明）

- 不承诺全局最优：迭代非线性最小二乘只给局部解；响应 JSON 含显式声明。
- 未实现稀疏求解/大规模图（纯稠密 NumPy，面向数百节点小规模问题）。
- 无前端、无可视化、无硬件/ROS 接入；数据全部为带固定种子的合成数据。
- ruff/black 因系统 PEP 668 保护无法在此环境安装；改用字节编译、
  AST 未使用导入扫描与 pytest 保证基本质量（扫描发现的 3 处测试冗余导入
  已清理；`__init__.py` 的 re-export 为预期误报）。
