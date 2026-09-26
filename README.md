# 二维位姿图优化（SE2 Pose Graph Optimization）— 纯后端

纯 Python + NumPy 实现的**离线**机器人位姿图非线性最小二乘计算库。
仅使用合成轨迹与合成传感器数据；**不连接硬件、不含可视化、不依赖 ROS**。

## 功能范围

- **SE(2) 位姿与相对约束**：位姿 `(x, y, θ)`，边测量 `z = (Δx, Δy, Δθ)` 定义在节点 i 的坐标系下；
  每条边携带 3×3 **信息矩阵 Ω**（协方差逆，要求半正定，自动对称化）。
- **非线性最小二乘目标**

  ```
  F(x) = Σ_e ρ_e( e_e(x)ᵀ Ω_e e_e(x) )
  ```

  残差

  ```
  e_xy = R(-θ_i)(t_j - t_i) - z_xy
  e_θ  = wrap(θ_j - θ_i - z_θ)
  ```

  其中角度残差每次求值都归一化到 `[-π, π)`，正确处理**跨越 ±π 切口**的角度
  （Jacobian 为局部 ±1，沿最短角方向收敛；见 `pose_graph/residual.py`，
  解析 Jacobian 已用中心有限差分校验）。
- **Gauss–Newton + Levenberg–Marquardt 阻尼**，失败步自动拒绝，保证记录的
  cost 历史**单调不增**。
- **鲁棒核（IRLS）**：`linear`（普通最小二乘）、`huber`、`cauchy`、
  `geman_mcclure`（Geman–McClure）。每条边可独立配置核类型与尺度参数，
  权重 `w = ρ'(s)` 逐次迭代缩放信息矩阵，用于抑制**错误回环约束（outlier）**。
- **规范自由度（gauge fixing）**：必须固定至少一个节点。未固定节点的连通分量
  会被**自动临时锚定**到该分量编号最小的节点，并在响应中报告；也可用
  `strict_anchoring` 选项改为直接报错。
- **连通性诊断**：并查集（union-find）划分连通分量，报告每个分量的节点、
  边数、固定节点以及未锚定分量。
- **JSON 入口**：请求/响应均为 JSON，提供命令行 `python -m pose_graph.cli`
  和库 API `run_from_dict`。

> **不承诺全局最优。** 迭代式非线性最小二乘只返回一个**局部最优解**；
> 初值不同可能收敛到不同局部极小。响应 JSON 中显式带有该声明。

## 目录结构

```
pose_graph/
  se2.py          SE2 位姿运算、角度归一化
  robust.py       鲁棒核 ρ(s) 与 IRLS 权重 ρ'(s)
  graph.py        节点/边数据结构、信息矩阵校验、并查集连通性诊断
  residual.py     边残差与解析 Jacobian（A、B 3×3 块）
  optimizer.py    Gauss-Newton / LM + IRLS 求解器
  synthetic.py    确定性合成轨迹（方形闭环、跨 π 角、长转角链、双分量）
  json_io.py      JSON 请求解析、校验、响应组装
  cli.py          命令行入口
examples/         JSON 请求样例 + responses/ 下实际运行的响应输出
tests/            pytest 自动化测试（43 项）
```

## 环境与安装

```bash
python3 -m venv .venv && source .venv/bin/activate   # 可选
pip install -r requirements.txt                       # 仅依赖 numpy
```

开发环境实测：Python 3.12.3、NumPy 2.5.3、pytest 9.1.1。

## 快速开始

命令行（结果打印到 stdout，或用 `-o` 写文件；退出码 0 成功 / 2 输入错误）：

```bash
python -m pose_graph.cli examples/square_loop.json -o response.json
```

库调用：

```python
from pose_graph.synthetic import build_square_graph
from pose_graph.optimizer import optimize, OptimizeOptions

graph, ground_truth = build_square_graph()      # 合成方形闭环 + 里程计漂移
result = optimize(graph, OptimizeOptions())
print(result.cost_initial, "->", result.cost_final)
print(result.termination_reason, result.connectivity["is_connected"])
```

## JSON 请求格式

```json
{
  "options": {"max_iterations": 100, "ftol": 1e-8, "xtol": 1e-10,
              "gtol": 1e-8, "lambda_init": 1e-3, "strict_anchoring": false},
  "nodes": [
    {"id": 0, "pose": [x, y, theta], "fixed": true}
  ],
  "edges": [
    {
      "i": 0, "j": 1,
      "z": [dx, dy, dtheta],
      "information": [[20,0,0],[0,20,0],[0,0,25]],
      "kernel": {"type": "geman_mcclure", "parameter": 1.0},
      "label": "loop_closure"
    }
  ]
}
```

`kernel.type` 缺省为 `linear`；非 linear 核必须提供正数 `parameter`。
输入不合法时返回 `{"status": "error", "error": {"type", "message"}}`。

响应含：`summary`（初/末 cost、迭代数、收敛标志、终止原因、固定与自动锚定
节点、残差 RMS、最大角度残差）、`poses`、`cost_history`、`connectivity`、
逐边 `edge_stats`（含每条边优化前后的平方/鲁棒残差）。

## 验收实验与实测结果

以下数字均来自本仓库实际运行（命令见末尾"复现实验"），合成数据固定随机种子。

### 1. 合成闭环：残差下降并恢复真值

20 节点方形闭环（`examples/square_loop.json`），初值为带噪里程计航位推算：

| 指标 | 数值 |
|---|---|
| cost 初始 → 最终 | 10.264 → 0.027（7 次迭代收敛） |
| 与真值最大平移误差 | < 0.3 m（边长 10 m） |
| 与真值最大角度误差（wrap 后） | ≈ 0.05 rad |
| cost 历史 | 单调不增（LM 拒绝劣化步） |

### 2. 错误回环约束 + 鲁棒核对比

注入一条错误回环（横向偏移 3 m、偏航 0.4 rad，权重与正常边相同，
初始平方马氏距离 s≈141）：

| 核 | cost 初始→最终 | 轨迹最大平移偏差 | 错误边最终 s |
|---|---|---|---|
| linear（无鲁棒） | 151.5 → 7.86 | **3.53 m**（被错误约束拉弯） | 1.50（被强行拟合） |
| huber (k=1) | 33.0 → 7.75 | 3.32 m | 2.20 |
| cauchy (k=1) | 15.2 → 4.89 | 1.08 m | 86.4（基本忽略） |
| geman_mcclure (k=1) | 11.3 → 1.02 | **0.29 m**（≈无坏边水平） | 184.4（保持大残差，被关断） |

**如实记录的局限**：Huber **不是重降（non-redescending）核**，其权重
`w = k/√s` 只降到一个随残差增大的小正数、永远不为 0，因此对这种量级
极大的错误回环只能"限幅"、不能彻底拒绝（k=0.2 时偏差仍有 1.57 m）。
需要对 gross outlier 近乎完全抑制时应选用 Cauchy 或 Geman–McClure。

### 3. 跨 ±π 角度处理

- `build_angle_cut_graph`：真实相对角 ≈ +π（在切口另一侧），初值在
  等价负角附近。未归一化的朴素残差约 6 rad（绕远路），归一化后仅约
  0.23 rad，沿短弧 4 次迭代收敛到 cost ≈ 0、最大角度残差 0。
- `build_long_turn_chain`：15 段链累计转角 6.75 rad（超过 2π），
  绝对航向穿越切口多次；优化后逐边角度残差全部 ≈ 0（终态绝对 θ 报告为
  0.467 rad，它与 6.75 rad 模 2π 等价，属正确结果）。

### 4. 图不连通诊断

两个互不相连的方形环（`examples/disconnected_two_components.json`）：
`is_connected=false`、`num_components=2`；分量 1 由用户固定节点 0，
分量 2 无固定节点 → 自动锚定节点 16（`auto_anchored_nodes=[16]`）后
分量独立优化，cost 2.369 → 0.014；`strict_anchoring=true` 时抛出
`GraphStructureError` 并列出未锚定分量。

## 运行测试

```bash
python -m pytest tests/ -v
```

当前结果：**43 passed**。覆盖 SE2 数学、鲁棒核性质、解析 Jacobian 对有限
差分、图校验与连通诊断、求解器全部验收场景、JSON/CLI 端到端（含错误退出码）。

## 复现实验

```bash
for f in examples/*.json; do
  python -m pose_graph.cli "$f" -o "examples/responses/$(basename ${f%.json}).response.json"
done
python -m pytest tests/ -q
```

## 设计说明与边界

- 固定节点通过在线性方程组中零化对应行列、对角置 1 实现；固定位姿严格不变。
- 无信息边、孤立节点都可以表示；孤立节点自成一个无锚分量（3 个未规范自由度），
  默认自动锚定其自身（即不移动）。
- 信息矩阵按对称部分 `(Ω+Ωᵀ)/2` 参与计算（残差二次型对反对称部分不敏感）。
- 纯 NumPy 稠密线性代数，面向**小规模**图（数百节点量级）；不包含稀疏求解、
  SLAM 前端、数据关联或可视化。
