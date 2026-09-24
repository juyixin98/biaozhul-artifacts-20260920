# SE(2) 二维位姿图优化服务

纯后端实现：输入相对位姿约束（含 3×3 信息矩阵），用稀疏 Gauss-Newton +
Huber 鲁棒核优化二维位姿图，通过 FastAPI 提供 HTTP 接口。无界面。

## 功能

- **SE(2) 误差模型**：边误差 `e = z⁻¹ ⊕ (xᵢ⁻¹ ⊕ xⱼ)`，角度残差归一化到
  `[-π, π)`（`pose_graph/se2.py: wrap_angle`，误差定义见
  `pose_graph/optimizer.py: edge_error_and_jacobians`）。
- **解析雅可比**：平移/旋转对两端位姿的解析导数，并用中心差分数值核对
  （`tests/test_jacobian.py`，200 组随机位姿）。
- **规范自由度固定**：锚定 `fix_node`（默认节点 0）的 3 个自由度
  （Hessian 对应块置为单位阵、右端置零）。
- **鲁棒核**：Huber 核（IRLS 权重 `w = ρ'(s)`，`s = eᵀΩe`），可选关闭
  （`robust_kernel: "none"`）。
- **稀疏求解**：法方程以 `scipy.sparse` 组装，`spsolve` 直接求解；
  带回溯线搜索保证代价单调下降。
- **报告**：每次迭代的代价与梯度范数（∞ 范数）、收敛信息，以及对锚定后
  Hessian 的特征值分析（最小特征值、条件数估计、退化判定）。

## 目录结构

```
pose_graph/
  se2.py         # SE(2) 位姿代数（compose / inverse / between / wrap_angle）
  optimizer.py   # 误差与雅可比、Huber 核、稀疏 GN、退化分析
  synthetic.py   # 合成数据：方形轨迹 + 里程计 + 正确/错误闭环
  models.py      # Pydantic 请求/响应模型（含信息矩阵 SPD 校验）
  app.py         # FastAPI 应用（/health, /optimize）
tests/           # pytest：SE2、数值雅可比、优化器端到端、API
examples/
  run_example.py       # 直接调用库的端到端示例（含错误闭环对比）
  sample_request.json  # /optimize 请求样例
requirements.txt       # 运行时依赖（锁定版本）
requirements-dev.txt   # 测试依赖（锁定版本）
```

## 安装与启动

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt        # 运行测试再装 -r requirements-dev.txt

# 启动服务（默认 8000 端口）
.venv/bin/uvicorn pose_graph.app:app --host 0.0.0.0 --port 8000
```

依赖：Python ≥ 3.10；fastapi 0.141.1、uvicorn 0.53.0、numpy 2.5.3、
scipy 1.18.1、pydantic 2.13.5（均已锁定，见 `requirements.txt`）。

## HTTP 接口

### `GET /health`

返回 `{"status": "ok"}`。

### `POST /optimize`

请求体（完整样例见 `examples/sample_request.json`）：

```json
{
  "initial_poses": [[0.0, 0.0, 0.0], [1.02, 0.03, 0.01]],
  "edges": [
    {
      "i": 0, "j": 1,
      "measurement": [1.0, 0.0, 0.0],
      "information": [[400, 0, 0], [0, 400, 0], [0, 0, 2500]]
    }
  ],
  "options": {
    "max_iterations": 50,
    "robust_kernel": "huber",
    "huber_delta": 1.0,
    "gradient_tolerance": 1e-6,
    "cost_tolerance": 1e-9,
    "fix_node": 0
  }
}
```

- `initial_poses`：N 个初始位姿 `[x, y, theta]`（弧度）。
- `edges[].measurement`：从节点 `i` 到节点 `j` 的相对位姿观测。
- `edges[].information`：3×3 信息矩阵，必须对称正定（否则 422）。
- `options.fix_node`：固定该节点以消除规范自由度（默认 0）。

响应：`optimized_poses`、`iterations`、`converged`、`cost_history`、
`gradient_norm_history`、`final_cost`、`final_gradient_norm`、
`degeneracy`（`is_degenerate` / 最小特征值 / 条件数估计 / 说明）。

调用示例：

```bash
curl -s -X POST http://127.0.0.1:8000/optimize \
  -H 'Content-Type: application/json' \
  -d @examples/sample_request.json | python3 -m json.tool
```

## 运行测试与示例

```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest tests/ -q     # 自动化测试
.venv/bin/python examples/run_example.py # 合成轨迹端到端示例
```

测试覆盖：

- `test_se2.py`：位姿代数（复合/求逆/结合律/角度归一化）。
- `test_jacobian.py`：解析雅可比 vs 中心差分（200 组随机位姿，
  `rtol=1e-4`），并验证一致位姿下误差为零。
- `test_optimizer.py`：含闭环合成轨迹上的收敛性、代价单调性、梯度下降、
  相对真值误差；错误闭环下 Huber 核 vs 普通最小二乘；不连通图的退化
  检测；`fix_node` 行为；非法参数。
- `test_api.py`：HTTP 端到端（含错误闭环）、非法信息矩阵 / 越界节点 /
  畸形位姿的 422 校验。

## 实测结果（本机，2026-09-24）

- `pytest tests/ -q`：**19 passed**（约 40 s）。
- `examples/run_example.py`（41 位姿、48 条边 + 4 条错误闭环，方形轨迹
  边长 10 m）：

| 场景 | 收敛 | 最终代价 | 平均位置误差 |
|---|---|---|---|
| 初始航迹推算 | — | — | 1.090 m |
| 干净图 + Huber | 是（15 次迭代） | 10.36 | 0.162 m |
| 含错误闭环，无核 | 是（9 次迭代） | 2561.05 | 0.840 m |
| 含错误闭环，Huber | 是（162 次迭代） | 278.80 | 0.340 m |

  错误闭环会显著拉偏普通最小二乘；Huber 核将其大幅抑制（0.84 m →
  0.34 m），但不能完全消除偏差（Huber 对极大残差仍保留 `δ/√s` 权重）。
- HTTP 服务实测：`uvicorn pose_graph.app:app` 启动后，`/health` 与
  `/optimize`（样例请求）均正常返回，收敛、代价/梯度历史与退化报告齐全。

## 已知限制 / 未完成项

- 鲁棒核仅实现 Huber；未实现 switchable constraints、DCS、
  truncated least squares 等更强的错误闭环剔除机制，因此错误闭环仍会
  残留少量偏差。
- 求解器为 Gauss-Newton + 回溯线搜索，未实现 Levenberg-Marquardt /
  Dogleg 信赖域；Huber 的 IRLS 权重使收敛变慢（上例需 ~160 次迭代）。
- 稀疏求解用 `spsolve`（直接法），未做增量式（iSAM 类）更新，大规模
  图（数万节点）未做性能测试。
- 退化分析对 ≤1200 维用稠密特征分解，更大规模回退到 `eigsh` 近似，
  仅作诊断用途。
