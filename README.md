# 稀疏轨迹平滑（纯后端）

离线轨迹平滑服务：在 **凸走廊（convex corridor）约束** 和 **固定端点** 下，最小化
「观测偏差 + 二阶差分平滑项」，返回平滑轨迹、可行性以及 KKT 最优性残差。
纯后端实现，无任何界面，通过 FastAPI 提供 HTTP 接口。

## 数学模型

给定观测点 `z_0 … z_{n-1}`（每点维度 `d`），求路径 `x_0 … x_{n-1}`：

```
min  Σ_i w_i · ‖x_i − z_i‖²  +  λ · Σ_{i=1}^{n-2} ‖x_{i−1} − 2 x_i + x_{i+1}‖²
s.t. x_i ∈ C_i,   C_i = { y : A_i y ≤ b_i }   （每个点各自的凸走廊，可为空集约束）
     x_0 = start,  x_{n-1} = end              （固定端点）
```

- `λ ≥ 0`：平滑权重（`smooth_weight`）。
- `w_i > 0`：各点观测权重，允许相差很多个数量级。
- 走廊是逐点、互不耦合的凸多面体；因此整体可行性可逐点判定。
  走廊为空、或固定端点落在自身走廊之外时，**明确返回不可行，且绝不输出路径**。
- 端点经变量消元后代入，内部点构成严格凸 QP（Hessian 正定），由
  `app/qp.py` 的**原始积极集法（primal active-set，Nocedal & Wright Alg. 16.3）**
  求解；初始可行点与可行性判定用 SciPy/HiGHS 线性规划。
- 成功时返回四个 KKT 残差：原始不可行度、稳定度（stationarity）、
  互补松弛度、对偶不可行度。

## 依赖

- Python 3.12
- numpy 2.5.3、scipy 1.18.1、fastapi 0.141.1、uvicorn 0.53.0
- 测试用 pytest / httpx

完整锁定版本见 `requirements.txt`（含全部传递依赖）。

## 安装与启动

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 启动服务
uvicorn app.main:app --host 0.0.0.0 --port 8000
# 交互式文档： http://127.0.0.1:8000/docs
```

## HTTP 接口

`POST /smooth`，JSON 请求体：

| 字段 | 类型 | 说明 |
|---|---|---|
| `observations` | `n×d` 数组，`n≥2` | 观测点 |
| `corridors` | 长度 `n` 的数组，可省略 | 每点 `{"A": [[…]], "b": […]}` 表示 `A y ≤ b`；空矩阵表示无约束 |
| `smooth_weight` | 数值，`≥0`，默认 1.0 | 二阶差分项权重 λ |
| `obs_weights` | 长度 `n` 数组或数值，默认 1.0 | 观测权重，须全部严格为正 |
| `start` / `end` | 长度 `d` 数组 | 固定端点，默认取首/末观测点 |

响应：`status`（`optimal` / `infeasible` / `iteration_limit` / `numerical_error`）、
`feasible`、`path`（不可行为 `null`）、`objective`、
`residuals {primal_infeasibility, stationarity, complementary_slackness, dual_infeasibility}`、
`iterations`、`message`。

`GET /health` 返回 `{"status": "ok"}`。

## 请求样例

```bash
curl -s http://127.0.0.1:8000/smooth \
  -H 'Content-Type: application/json' \
  -d @examples/example_request.json | python -m json.tool
```

或：

```bash
python examples/run_example.py
```

不可行（自相矛盾的走廊：`x≤0` 且 `x≥1`）示例：

```bash
curl -s http://127.0.0.1:8000/smooth -H 'Content-Type: application/json' -d '{
  "observations": [[0,0],[1,1],[2,2],[3,3]],
  "corridors": [
    {"A": [], "b": []},
    {"A": [[1,0],[-1,0]], "b": [0,-1]},
    {"A": [], "b": []},
    {"A": [], "b": []}
  ]
}'
# -> {"status":"infeasible","feasible":false,"path":null, ...}
```

## 测试

```bash
pip install pytest httpx        # 已包含在 requirements.txt
python -m pytest tests/ -v
```

测试覆盖（共 36 个用例）：

- **与独立通用求解器对照**：随机 QP 与完整轨迹问题均用 SciPy 的
  SLSQP（SQP 通用约束求解器，与自研积极集法相互独立）复算，比较目标值。
- **互相冲突的走廊**：空多面体、固定端点越界，均判为不可行且不返回路径。
- **重复点**：含大量重复观测时仍收敛、约束满足、与 SLSQP 一致。
- **权重跨度**：观测权重跨越 12 个数量级（1e-6…1e6）时解仍满足 KKT，
  高权重点几乎被精确跟踪。
- λ=0、仅 2 个点、退化/重复约束、HTTP 入参校验等边界情况。

## 目录结构

```
app/
  qp.py        # 通用严格凸 QP：原始积极集法 + HiGHS 可行性判定
  smoother.py  # 轨迹平滑问题装配、逐点可行性检查、KKT 残差
  main.py      # FastAPI 接口与请求校验
tests/         # QP 求解器 / 平滑器 / HTTP 接口测试
examples/      # 请求样例与调用脚本
requirements.txt
```

## 实际运行结果

- `python -m pytest tests/ -q`：**36 passed**（Python 3.12.3，numpy 2.5.3 / scipy 1.18.1）。
- 已实际启动 uvicorn 并用 curl / 示例脚本验证可行与不可行请求（见下方提交记录中的实测输出）。

## 未完成 / 限制项

- 积极集法最坏复杂度随约束数指数增长，适合离线中小规模实例；超大规模（上万点）
  应换用内点法或稀疏结构化求解器，本项目未实现。
- 走廊仅支持多面体 `A y ≤ b`（凸走廊最常见形式）；不支持二次/圆盘约束。
- 未做鉴权、限流等服务化治理（按需求仅做纯后端算法服务）。
