# 二维位姿图优化（SE2 Pose-Graph Optimization）纯后端

用 **Python + NumPy** 实现的小规模 **SE2 非线性最小二乘位姿图优化**离线计算库，
带 **JSON 文件入口**。只使用程序化生成的合成轨迹与传感器数据，
**不连接任何硬件、不做任何可视化、不依赖 ROS**。

> ⚠️ 非线性最小二乘只保证收敛到**局部最优**，本项目**不承诺全局最优**。
> 当初始猜测远离正确解或错误约束权重过大时，结果可能停在其他局部极小点。

## 功能

- SE2 位姿与相对位姿约束：`T_j ≈ T_i · Z_ij`，残差表达在测量坐标系下；
- Gauss-Newton / **Levenberg-Marquardt** 求解，解析雅可比（有限差分校验）；
- 每条边带 **3×3 信息矩阵** Ω（= 协方差之逆），校验对称/正定；
- **固定一个节点**规范全局 (x, y, θ) 三个不可观自由度；
- **鲁棒核**（IRLS 加权）抑制错误回环：Huber / Cauchy / Tukey(bisquare)；
- 角度残差统一 `wrap` 到 `(-π, π]`，正确处理**角度跨 ±π**（branch cut）；
- **图不连通 / 无固定节点 / 空图 / 半定信息矩阵**的求解前诊断；
- 合成数据：正方形闭环、整圆（跨 π）轨迹、开环直线、不连通双链；
- JSON 请求/响应 + CLI（`solve` / `demo` / `list-scenarios`）。

## 目录结构

```
pose_graph_optimizer/
  se2.py         # SE2 运算：wrap、复合、求逆、残差、解析雅可比
  kernels.py     # Huber / Cauchy / Tukey 鲁棒核（权重与代价）
  graph.py       # 节点/边/位姿图、并查集连通分量、信息矩阵工具
  optimizer.py   # LM 非线性最小二乘求解器 + 结构诊断
  synthetic.py   # 合成轨迹/里程计/回环/错误回环生成
  io_json.py     # JSON 请求解析、校验、响应序列化
  cli.py         # 命令行入口
examples/        # 请求样例与已生成的响应样例
tests/           # pytest 自动化测试（68 个用例）
```

## 安装

```bash
python3 -m pip install -r requirements.txt   # 仅需 numpy
```

## 快速开始

### 1) 用 JSON 请求求解

```bash
python3 -m pose_graph_optimizer solve -r examples/request_small_loop.json \
    -o examples/response_small_loop.json -v
```

### 2) 内置合成场景

```bash
python3 -m pose_graph_optimizer list-scenarios
python3 -m pose_graph_optimizer demo -s square -v
python3 -m pose_graph_optimizer demo -s circle_wrap
python3 -m pose_graph_optimizer demo -s disconnected   # 预期诊断失败，退出码 1
```

### 3) 作为库调用

```python
from pose_graph_optimizer.synthetic import standard_scenarios
from pose_graph_optimizer.optimizer import optimize, GraphNotSolvedError

graph, truth = standard_scenarios(seed=42)["square"]["builder"]()
try:
    result = optimize(graph)
except GraphNotSolvedError as exc:
    print("图结构问题:", exc)
else:
    print(result.initial_cost, "->", result.final_cost)
```

## JSON 请求格式

```json
{
  "name": "可选名称",
  "options": {"max_iterations": 50, "tol_step": 1e-8, "initial_lambda": 0.001},
  "nodes": [
    {"id": 0, "pose": [x, y, theta], "fixed": true}
  ],
  "edges": [
    {
      "i": 0, "j": 1,
      "measurement": [dx, dy, dtheta],
      "info": [[400,0,0],[0,400,0],[0,0,821]],
      "kernel": {"type": "huber", "delta": 1.0}
    }
  ]
}
```

- `measurement`：局部相对位姿 `Z_ij`，含义 `T_j ≈ T_i · Z_ij`；
- `info`：可直接给 3×3 正定对称矩阵，也可给噪声参数
  `{"sigma_xy": 0.05, "sigma_theta": 0.035, "corr": 0.0}` 自动展开；
- `kernel`：可省略（普通最小二乘），`type` ∈ `none/huber/cauchy/tukey`；
- 至少一个节点 `"fixed": true`，且图必须连通，否则返回 `success=false` 诊断。

响应包含：收敛状态、迭代次数、初/终代价、各节点最终位姿、
每条边的 χ² 与鲁棒权重、代价下降历史、连通分量与诊断信息。

## 数学说明

单边残差（表达在测量坐标系）：

```
e_ij = log( Z_ij^{-1} · T_i^{-1} · T_j )
     = [ Rz^T ( Ri^T (tj - ti) - tz ) ]
       [ wrap(θj - θi - θz)            ]
```

目标函数（鲁棒化加权最小二乘）：

```
F(X) = Σ_edges 0.5 · ρ( sqrt(e_k^T Ω_k e_k) )
```

每轮迭代把残差马氏距离代入核函数得到 IRLS 权重 `w = ρ'(r)/r`，
将信息矩阵缩放为 `w·Ω`，再解线性化法方程：

```
(J^T (w Ω) J + λ diag(J^T (w Ω) J)) Δ = -J^T (w Ω) e
```

- 固定节点对应行列清零、对角置 1，其更新恒为 0；
- 角度分量经 `wrap` 到 `(-π, π]`，跨 ±π 时走最短角差；
- LM 内层做阻尼放大搜索，仅接受代价严格下降的步长；
  步长小于阈值即判驻点收敛。

### 鲁棒核的行为差异（实测，见 `RUN_REPORT.md`）

- **Tukey(bisquare)**：残差超过 `delta` 后权重直接为 **0**（硬拒绝），
  错误回环几乎不影响解；
- **Huber**：大残差权重 `δ/r`，只降不归零（软抑制），在 IRLS 下
  强错误约束仍可能把解缓慢拉偏；
- **Cauchy**：权重 `1/(1+(r/δ)²)`，同样不归零。
  建议需要剔除错误回环时优先使用 Tukey 并合理选择 `delta`。

## 运行测试

```bash
python3 -m pytest -q
python3 -m pytest --cov=pose_graph_optimizer --cov-report=term-missing
```

当前：**68 个用例全部通过，行覆盖率 94%**（实测见 `RUN_REPORT.md`）。

## 已知边界 / 非目标

- 定位为**小规模**教学/离线后端：稠密 `numpy.linalg.solve` 直接组装法方程，
  未使用稀疏求解器，不适合上千节点的大规模图；
- 不做全局最优保证、不做前端/可视化、不接真实传感器与 ROS；
- 不连通图当前一律拒绝（即使每个分量各自固定了节点），以给出明确诊断。
