# SE(2) 二维位姿图优化服务

纯后端服务：给定带初始估计的 SE(2) 位姿节点、相对位姿约束（里程计 / 闭环）及每个约束的
3×3 信息矩阵，使用**鲁棒核 + 稀疏 Gauss-Newton / Levenberg–Marquardt**求解最大似然位姿图。
默认固定首节点以消除全局 SE(2) 规范自由度；角度残差始终归一化到 `[-π, π)`。

- Web 框架：FastAPI（自动生成 `/docs` Swagger UI）
- 数值栈：NumPy + SciPy（`scipy.sparse` 组装、`spsolve` 稀疏求解、CSGraph 连通性 / ARPACK 谱分析）
- 无前端，无外部优化器依赖

## 目录结构

```
app.py                     # FastAPI 应用（POST /optimize, GET /health）
pgo/
  se2.py                   # SE(2) 几何：compose/inverse/between、角度归一化
  kernels.py               # 鲁棒核：none / huber / cauchy（g2o 约定）
  optimizer.py             # 残差、解析雅可比、稀疏 LM/GN、退化诊断
  synthetic.py             # 合成轨迹（里程计 + 正确闭环 + 错误闭环）
examples/
  run_demo.py              # 数值差分核对雅可比 + 含错误闭环的端到端验证
  request_loop.json        # HTTP 请求样例
tests/                     # pytest：SE(2) 恒等式、雅可比、优化器、HTTP 接口
requirements.txt           # 锁定依赖（pip freeze）
```

## 环境与启动

Python 3.12。

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 启动 HTTP 服务（默认 :8000）
.venv/bin/uvicorn app:app --host 0.0.0.0 --port 8000
```

交互式 API 文档：<http://localhost:8000/docs>（可直接发请求）。

## 请求格式（`POST /optimize`）

```json
{
  "poses": [[x, y, theta_rad], ...],
  "edges": [
    {
      "i": 0,
      "j": 1,
      "measurement": [dx, dy, dtheta_rad],
      "information": [[100,0,0],[0,100,0],[0,0,400]]
    }
  ],
  "options": {
    "kernel": "huber",          // none | huber | cauchy
    "kernel_delta": 1.0,        // 白化残差尺度上的核阈值
    "max_iterations": 50,
    "fixed_nodes": [0]          // 默认固定节点 0
  }
}
```

- `measurement` 是节点 `i` 坐标系下的相对位姿 `T_i⁻¹ T_j`。
- `information` 必须对称正定（非 PD 返回 HTTP 422）。
- 响应包含：优化后位姿、初始/最终代价、初始/最终梯度 ∞-范数、逐次迭代记录、
  每条边的鲁棒代价、连通分量数、正规矩阵最小/最大特征值、退化标志。

### curl 样例

```bash
curl -s http://localhost:8000/health
curl -s -X POST http://localhost:8000/optimize \
  -H 'Content-Type: application/json' \
  --data @examples/request_loop.json | python3 -m json.tool
```

## 数学模型

边 `i→j`（测量 `Z`，信息矩阵 `Ω`）的残差（等价于 g2o `EdgeSE2`）：

```
e = [ E.tx, E.ty, wrap(E.θ) ],   E = Z⁻¹ ( T_i⁻¹ T_j )
```

角度分量 `wrap(θ_j − θ_i − θ_z)` 归一化到 `[-π, π)`，这是线性化求解器跨角度分支
仍能正确工作的关键（有专门单元测试覆盖 ±π 附近的情形）。

代价与求解：

```
总代价 = Σ_k ρ(s_k),   s_k = e_kᵀ Ω_k e_k
```

- 鲁棒核按 IRLS 处理：每轮将边信息加权为 `ρ'(s)·Ω`（none / Huber / Cauchy，g2o 约定）。
- 正规方程 `(Jᵀ W J + λI) Δx = −Jᵀ W e` 稀疏组装后用 `scipy.sparse.linalg.spsolve` 求解；
  λ 按 LM 规则自适应，拒绝使代价上升的步。
- 雅可比为**解析形式**（`pgo/optimizer.py` 中 `edge_jacobians`），并通过中心数值差分核对。

退化诊断：CSGraph 连通分量（多分量即存在未约束刚体自由度）＋自由变量正规矩阵的
最小特征值（相对最大特征值过小判定病态/退化）。

## 验收方法与实测结果

### 1) 自动化测试

```bash
.venv/bin/python -m pytest tests/ -q
```

实测：**44 passed**（SE(2) 恒等式与角度回绕、25 组随机位姿的解析/数值雅可比、
优化器收敛与恢复真值、错误闭环下鲁棒核优于最小二乘、断开图的退化报告、
HTTP 200/422 用例）。

### 2) 合成轨迹端到端验证

```bash
.venv/bin/python examples/run_demo.py
```

场景：5×1=5 m 的矩形闭环，20 个节点；带噪声里程计 19 条 + 1 条正确闭环 +
1 条**错误闭环**（感知误匹配，平移偏差 1.5 m、角度偏差 0.6 rad）。实测：

- 雅可比中心差分核对：平移块最大误差 `~1e-8`，角度块 `~6e-9`。
- 纯最小二乘被错误闭环拉偏：RMSE(x,y) ≈ **0.70 m**。
- Huber(δ=1)：RMSE ≈ **0.30 m**；错误闭环边的鲁棒代价比正常边中位数大两个数量级。
- Cauchy(δ=1)：RMSE ≈ **0.07 m**，接近无错误闭环时的噪声下限 ≈ 0.056 m。
- 代价单调下降，最终梯度 ∞-范数 ≈ 1e-5 以下，连通分量 = 1，非退化。

（精确数字随脚本运行打印；上述为本环境一次实测。）

## 已知限制 / 未完成项

- 求解器为 CPU 上的稀疏 LM；未接入 CHOLMOD/SuiteSparse（用的是 SciPy SuperLU），
  数万节点以上规模可能不是最优选择。
- 仅支持 SE(2)；未实现 SE(3)。
- 无前端 / 可视化（按需求只做后端）；`pgo.synthetic` 提供程序化取数与 RMSE 评估。
- 固定节点策略默认锚定节点 0；目前不支持 GPS 等先验因子（可作为单边约束扩展）。
- 谱诊断在大图上使用 ARPACK，极端病态时可能报 `None`（此时以连通性结果为准）。
