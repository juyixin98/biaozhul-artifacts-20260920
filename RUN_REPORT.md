# 运行报告（RUN_REPORT）

本报告记录在交付环境中**实际执行**的命令与结果。所有数据为真实输出，未做修饰。

- 日期：2026-09-25
- 环境：Linux 6.8.0-90-generic，Python 3.12.3，NumPy 2.5.3，pytest 9.1.1
- 依赖：仅 `numpy`（`requirements.txt`）；测试额外使用已安装的 `pytest`、`pytest-cov`

## 1. 自动化测试

命令：

```bash
python3 -m pytest -q
```

结果：

```
.................................................................... [100%]
68 passed in 1.16s
```

覆盖率：

```bash
python3 -m pytest --cov=pose_graph_optimizer --cov-report=term -q
```

```
Name                                Stmts   Miss  Cover
pose_graph_optimizer/__init__.py        6      0   100%
pose_graph_optimizer/__main__.py        2      2     0%   # 仅 3 行引导，已被 subprocess 用例间接执行
pose_graph_optimizer/cli.py            95      4    96%
pose_graph_optimizer/graph.py         104      2    98%
pose_graph_optimizer/io_json.py       154     16    90%
pose_graph_optimizer/kernels.py        43      0   100%
pose_graph_optimizer/optimizer.py     175     14    92%
pose_graph_optimizer/se2.py            49      0   100%
pose_graph_optimizer/synthetic.py     100      4    96%
TOTAL                                 728     42    94%
68 passed
```

行覆盖率 **94% ≥ 80%** 验收线。

## 2. JSON 入口实测

```bash
python3 -m pose_graph_optimizer solve -r examples/request_small_loop.json -o ...
# 退出码=0 | 状态: converged_cost | 迭代 4 次 | 代价 97.030869 -> 85.356629 (下降 12.03%)

python3 -m pose_graph_optimizer solve -r examples/request_square.json -o ...
# 退出码=0 | 状态: converged_cost | 迭代 6 次 | 代价 4.634123 -> 1.701540 (下降 63.28%)

python3 -m pose_graph_optimizer solve -r examples/request_disconnected.json -o ...
# 退出码=1 | 图结构诊断失败: 图不连通：共 2 个连通分量
#   （分量1: 5 个节点 [5..9]; 分量2: 5 个节点 [0..4]）……
```

退出码约定：`0` 收敛；`1` 图结构诊断失败（响应仍写出，`success=false`）；
`2` 请求参数/JSON 错误；`3` 迭代未收敛。

## 3. 验收项逐项核对

### 3.1 合成闭环 + 残差下降

`demo -s square`（25 节点正方形，24 里程计边 + 1 正确回环 + 1 错误回环）：

```
[iter 0] cost=4.63418207
[iter 1] cost=1.74015192 step=4.349e-01 rel_drop=6.245e-01
[iter 2] cost=1.70257236
[iter 3] cost=1.70154534
[iter 6] cost=1.70154135 step=3.648e-07
对真值最大误差: 平移 0.1939 m，角度 3.324°
```

- 代价历史严格单调不增（测试 `test_cost_is_monotone_nonincreasing` 断言）；
- 对照实验（33 节点大噪声方框，σxy=0.1m, σθ=3°, seed=0）：
  纯里程计最大漂移 **1.80m**，加入正确回环后 **0.54m**（误差降至约 30%）；
- 无噪声完美数据 + 扰动初值：最终代价 `< 1e-12`，位姿与真值一致。

### 3.2 鲁棒核处理错误回环

- square 错误回环（注入偏移 [1.5m, -1.0m, 0.4rad]，Tukey δ=3）：
  最终 `robust_weight = 0.0`，被硬拒绝；拒绝后精度 0.194m，
  与“根本不放错误回环”的 0.194m 一致。
- circle 错误回环（Tukey δ=4）：权重同样归零，对真值误差 0.38m。
- 对照组：关闭鲁棒核后错误回环使 square 最大误差升至 **1.64m**。

实测中记录到一个**真实的核函数行为差异**（已写入 README，非缺陷）：
Huber / Cauchy 的权重只降不归零，在 IRLS 迭代下强错误约束可能被逐步“拉入”
解（circle 上 Cauchy 把错误回环拟合到 χ²≈1，轨迹偏差 0.89m）；
需要真正剔除错误回环时应使用 **Tukey**。因此内置场景统一用 Tukey 做硬拒绝，
小手工样例保留 Huber 展示软抑制（权重 0.012，未归零）。

### 3.3 角度跨 ±π（branch cut）

- 单元构造：初始角 −2.8 rad、测量 +2.8 rad（二者差 2π，本是同一朝向，
  最短角差仅 0.68 rad）。若角度不 wrap，线性化会沿错误方向更新；
  实测 4 次迭代收敛，最终代价 `≈ 4e-31`，结果朝向与 +2.8 等价。
- `circle_wrap`：整圆轨迹朝向累计 2π（跨过 +π），正确回环边 0→40
  最终**角度残差仅 −0.0012 rad**，而不是接近 ±2π 的伪误差；
  各节点朝向经 wrap 后与真值一致（最大偏差在噪声范围内）。

### 3.4 图不连通诊断

`disconnected` 场景（两条独立链，仅主分量固定节点 0）在**求解前**被拒绝：

```
GraphNotSolvedError: 图不连通：共 2 个连通分量（分量1: 5 个节点 [5,6,7,8,9];
分量2: 5 个节点 [0,1,2,3,4]）。不含固定节点的分量整体平移/旋转不可观，
法方程将奇异；请补充连接边或为每个分量固定一个节点。
```

另有诊断：空图、无固定节点、信息矩阵非正定/不对称；
半定（只约束角度）信息矩阵在求解时返回 `status=singular_system` 而非崩溃。

### 3.5 全局最优

本项目为非线性最小二乘，只保证在给定初值下的**局部收敛**，
README、响应 JSON 的 `diagnostics.note` 与结果类文档中均明确
**“不承诺全局最优”**，未在任何地方宣称全局最优。

## 4. 未通过项 / 已知限制

- **无未通过的验收项**：68 个测试全部通过，四类验收行为均可复现。
- 开发过程中发现并已修复的两个真实问题（记录以示如实）：
  1. 当代价已为 0（初值即最优）时，旧逻辑因“要求代价严格下降”误判为
     `no_descent`；已增加“GN 步长 < tol_step 即驻点收敛”判定；
  2. `wrap_angle(-π)` 原返回 `−π`，与文档约定的区间 `(-π, π]` 不符；
     已改为返回 `+π`（同一朝向）。
- 已知设计边界：稠密法方程求解，面向小规模（数十节点）教学/离线场景，
  无稀疏求解器；不做前端、可视化、硬件或 ROS 对接；
  不连通图一律拒绝（即使各分量各自固定），以给出明确诊断。
