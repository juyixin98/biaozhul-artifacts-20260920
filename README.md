# 小规模单目束调整（Bundle Adjustment）后端

纯后端实现的小规模离线单目束调整：优化相机位姿与三维点，固定内参，
锚定首相机与尺度，使用 **Schur 补消元** 求解正规方程。
仅依赖 NumPy / SciPy / FastAPI，**不调用任何现成 SLAM 系统**。

## 功能

- 针孔投影模型（无畸变），内参固定；
- 相机位姿以局部 se(3) 左扰动参数化，Levenberg-Marquardt 迭代；
- 两种正规方程求解方式，可互换对照：
  - `schur`：消元三维点，解相机约化方程后回代；
  - `full`：直接求解完整正规方程（验收对照用）；
- 规范（gauge）处理：固定首相机（6 自由度）+ 尺度锚定
  （对 `||t_1||` 施加强先验并在每次接受步长后硬投影回目标值）；
- 边界情况处理与诊断：
  - 负深度观测（点在相机后方）自动剔除并计数；
  - 观测不足（被少于 2 台相机看到的点）在诊断中标记，LM 阻尼保证可解；
  - 尺度歧义：关闭锚定时代价对全局尺度不变（测试验证）。

## 目录结构

```
ba/                 核心库
  lie.py            SO(3) 指数/对数映射（Rodrigues）
  projection.py     投影模型与解析雅可比
  problem.py        数据结构（Intrinsics / CameraPose / Observation / BAProblem）
  solver.py         LM 求解器（Schur 补 + 完整正规方程）
  simulate.py       合成场景生成（真值已知，投影加噪）
main.py             FastAPI 应用（HTTP 接口）
tests/              pytest 自动化测试（18 项）
examples/           客户端示例与 curl 样例
requirements.txt    锁定依赖
```

## 安装与启动

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 运行测试
.venv/bin/python -m pytest tests/ -v

# 启动服务（默认 8000 端口）
.venv/bin/uvicorn main:app --port 8000
```

交互式 API 文档：启动后访问 `http://127.0.0.1:8000/docs`。

## HTTP 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET  | `/health` | 健康检查 |
| POST | `/v1/simulate` | 生成带噪合成问题（返回问题 + 真值） |
| POST | `/v1/solve` | 求解给定问题，`options.solver` 选 `schur`/`full` |
| POST | `/v1/solve_simulated` | 一键：生成场景并求解 |

### 请求样例

```bash
# 一键仿真 + Schur 补求解
curl -X POST http://127.0.0.1:8000/v1/solve_simulated \
  -H 'Content-Type: application/json' \
  -d '{"scene":  {"n_cameras": 5, "n_points": 40, "noise_px": 1.0, "seed": 0,
                  "n_behind_camera": 2, "n_under_observed": 2},
       "options": {"solver": "schur", "max_iterations": 50}}'
```

自定义问题（相机用 Rodrigues 旋转向量 + 平移向量表示，世界到相机）：

```bash
curl -X POST http://127.0.0.1:8000/v1/solve \
  -H 'Content-Type: application/json' \
  -d '{
    "problem": {
      "intrinsics": {"fx": 500, "fy": 500, "cx": 320, "cy": 240},
      "cameras": [{"rvec": [0,0,0], "tvec": [0,0,0]},
                  {"rvec": [0,0.01,0], "tvec": [-0.9,0,0]}],
      "points": [[0.1, 0.0, 5.0]],
      "observations": [{"camera": 0, "point": 0, "uv": [330.0, 240.0]},
                       {"camera": 1, "point": 0, "uv": [340.0, 240.0]}]
    },
    "options": {"solver": "full"}
  }'
```

更多样例见 `examples/curl_examples.sh` 与 `examples/client_example.py`
（`python examples/client_example.py`，需服务已启动）。

## 实测结果（本机实际运行记录）

环境：Python 3.12.3，numpy 2.5.3，scipy 1.18.1，fastapi 0.141.1。

- **自动化测试**：`pytest tests/` —— **18 项全部通过**（含雅可比有限差分
  校验、Schur 与完整正规方程单步/全程一致性、负深度剔除、观测不足标记、
  尺度歧义与锚定恢复、API 端到端）。
- **示例运行**（5 相机 / 40 点 / 1 px 噪声 / 2 负深度点 / 2 观测不足点）：
  - 7 次 LM 迭代收敛，代价 162561.13 → 139.68（≈ 观测数 × 噪声方差量级）；
  - Schur 与完整正规方程最终代价相对差异 **8.14e-16**（机器精度量级）；
  - 负深度观测剔除 2 个；观测不足点标记 4 个（含 2 个负深度点，其唯一观测
    被剔除后有效观测为 0）；
  - 尺度锚定 `||t_1||`：目标 0.548210，终值 0.548210（硬投影精确贴合）。
    注意锚定目标是**初始值**的 `||t_1||`（真值加扰动），因此与真值
    0.502063 存在初始扰动量级的偏差，这符合"锚定初值尺度"的语义；
    若传入 `scale_target` 为真值尺度，则恢复到真值尺度（测试覆盖）。

## 已知限制 / 未完成项

- 未实现鲁棒核（Huber 等），对离群观测敏感；
- 未做稀疏存储（块用稠密 NumPy 数组），定位是"小规模"，点数上千后应换
  `scipy.sparse`；
- 尺度锚定采用"强先验 + 硬投影"，非流形上的精确约束优化；
- 无畸变模型、无滚动快门等实际相机因素；
- 观测不足的点仅做诊断标记，未从优化中自动移除（依赖 LM 阻尼保持可解）。
