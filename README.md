# 约束轨迹平滑服务 (Constrained Trajectory Smoothing)

纯后端离线二维折线轨迹平滑服务。给定折线路径、固定端点和矩形障碍，在**同时限制曲率、偏离原路径、最小边长和障碍净空**的前提下，用
SciPy SLSQP 真正求解一个约束非线性优化问题，并对**整段轨迹**做密集碰撞验证。

任何不可行、未收敛、超时或验证失败的情况，服务都**返回原始路径和失败状态**，
绝不发布一条未经验证的新路径。没有用固定的平滑曲线/样条冒充求解结果。

## 1. 它解决什么问题

决策变量是折线的内部顶点坐标（端点固定）。最小化目标：

```
min  w_dev · Σ‖p_i − p_i^orig‖²          (偏离原路径)
   + w_bend · Σ‖Δ²p_i‖²                  (二阶差分 / 弯曲能量)
   + w_jerk · Σ‖Δ³p_i‖²                  (三阶差分 / 抖动)
```

硬约束（残差约定：`limit − observed ≥ 0` 即满足）：

| 约束 | 内容 |
|---|---|
| 固定端点 | 首末顶点与输入逐位一致 |
| 偏离走廊 | 每个内部顶点落在原顶点为圆心、`corridor` 为半径的圆盘内 |
| 曲率上限 | 每个连续三点的 **Menger 曲率** ≤ `curvature_cap`（解析雅可比） |
| 最小边长 | 每条控制段长度 ≥ `min_edge_length`（防折叠退化） |
| 障碍净空 | 每个控制顶点 **和每条控制段中点** 相对每个矩形的有符号距离 ≥ `clearance` |

求解在**归一化坐标系**（路径+障碍包围盒对角线归一为 1）中进行，因此在
毫米、米、千米等不同尺度下结果一致（有跨 6 个数量级的测试）。

### 整段轨迹碰撞验证（不只检查控制点）

求解返回后，独立地对最终轨迹执行两道验收（`app/geometry.py::verify_trajectory`）：

1. **自适应密集采样**：每条控制段按 ≤ `clearance/2`（且单边最多 200 等分）
   细分，逐点计算矩形有符号距离；所有折角顶点必在采样集中。
2. **精确线段-矩形检测**：对每条控制段做 Liang–Barsky 裁剪求精确有符号距离，
   能抓住“夹在两个控制点之间、整段穿入障碍”的碰撞——这种碰撞只检查控制点
   必然漏检（有专门测试）。

任一道不过 → 返回原路径 + 失败状态。

## 2. 目录结构

```
app/
  geometry.py    矩形 SDF、SDF 解析梯度、线段-矩形精确距离、密集验证、重复点折叠
  smoother.py    SLSQP 约束优化、Menger 曲率及解析雅可比、失败回退、残差块
  schemas.py     Pydantic 请求/响应协议
  service.py     请求→求解→响应、NaN/Inf 清洗、运行记录落盘
  integrity.py   真实 SHA-256（标准 JSON 规范化后哈希）
  main.py        FastAPI 应用
examples/        窄通道 / 障碍切角 / 重复点 / 100点毫米尺度 / 不可行 夹具
tests/           51 个自动化测试（几何、雅可比有限差分核对、端到端、HTTP）
scripts/smoke.py 快速冒烟脚本
requirements.txt 直接依赖（带版本区间）
requirements.lock 完整锁定版本（本仓库实际验证通过的环境）
```

## 3. 本地启动

需要 Python 3.11+（开发于 3.12）。

```bash
cd /home/admin/Downloads/biaozhul/P067/a
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.lock     # 或 pip install -r requirements.txt
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

健康检查：

```bash
curl -s http://127.0.0.1:8000/health
```

交互文档（Swagger UI）：<http://127.0.0.1:8000/docs>

## 4. 验收命令

```bash
# 1) 全部自动化测试（几何/雅可比/夹具/HTTP，共 51 个）
source .venv/bin/activate
python -m pytest -q

# 2) 快速冒烟（窄通道 + 障碍切角直接走优化器）
PYTHONPATH=. python scripts/smoke.py

# 3) 窄通道夹具
curl -s http://127.0.0.1:8000/api/v1/smooth \
  -H 'Content-Type: application/json' \
  --data @examples/narrow_corridor.json | python -m json.tool

# 4) 障碍切角夹具（对角线弦会切角穿障碍，验证解仍然绕外侧且碰撞安全）
curl -s http://127.0.0.1:8000/api/v1/smooth \
  -H 'Content-Type: application/json' \
  --data @examples/corner_cut.json | python -m json.tool

# 5) 不可行夹具（半径要求超过走廊 → 必须返回原路径和失败状态）
curl -s http://127.0.0.1:8000/api/v1/smooth \
  -H 'Content-Type: application/json' \
  --data @examples/infeasible.json | python -m json.tool

# 6) 100 点、毫米尺度夹具（点数上限与尺度鲁棒性）
curl -s http://127.0.0.1:8000/api/v1/smooth \
  -H 'Content-Type: application/json' \
  --data @examples/large_scale_mm.json | python -m json.tool
```

### 成功响应的关键判定字段

```jsonc
{
  "status": "optimal",          // optimal | infeasible | iteration_limit | timeout | input_collision | ...
  "success": true,
  "points": [[x, y], ...],      // 失败时与输入 path 完全一致（不发布非法路径）
  "point_count": 7,
  "fixed_start": [0.0, 0.0],    // 与输入首点一致
  "fixed_goal":  [10.0, 10.0],
  "iterations": 11,
  "max_iterations": 200,        // 明确的迭代预算
  "objective": 0.029,
  "objective_components": { "deviation_sum_sq": ..., "bend_sum_sq": ..., "jerk_sum_sq": ... },
  "residuals": {                // 目标函数与约束残差（物理单位 + 归一化单位）
    "max_violation": -0.0,      // ≥0 表示全部硬约束满足
    "residuals_physical": { "deviation": ..., "curvature": ...,
                            "min_edge_length": ..., "clearance": ...,
                            "fixed_endpoints": ... },
    "observed_physical": { ... }
  },
  "verification": {             // 整段轨迹的密集碰撞验收
    "ok": true,
    "min_clearance": 0.781,
    "samples_checked": 923,
    "control_segments_checked": 6,
    "sample_spacing": 0.025,
    "worst_sample": [7.13, 4.12]
  },
  "collapsed_duplicates": 0,
  "request_sha256": "…",        // 对收到的原始请求 JSON 的真实 SHA-256
  "response_sha256": "…"        // 对响应体（除该字段外）的真实 SHA-256
}
```

失败时 `success=false`，`points` 原样回传，`verification` 可能为 `null`，
`residuals` 仍然给出求解失败点的约束违反量。

## 5. 请求协议 `POST /api/v1/smooth`

```json
{
  "request_id": "可选字符串",
  "path": [[x, y], ...],                 // 2–100 个点，硬上限 100
  "obstacles": [
    {"cx": 5.0, "cy": 5.0, "width": 4.0, "height": 4.0}   // 轴对齐矩形，宽高必须 >0
  ],
  "params": {
    "max_iterations": 200,               // 迭代预算，1–1000
    "timeout_seconds": 30.0,             // 墙钟预算
    "curvature_cap": 0.8,                // 1/长度；null=输入最大曲率的 60%
    "corridor": 1.5,                     // 长度；null=包围盒对角线的 25%
    "clearance": 0.05,                   // 障碍净空，≥0
    "min_edge_length": null,             // null=最小原始非零边长的 35%
    "deviation_weight": 1.0,
    "bend_weight": 0.0,
    "jerk_weight": 0.05,                 // 三个权重至少一个 >0
    "optimize_midpoint_clearance": true  // 同时约束控制段中点净空
  }
}
```

约束参数的物理可行性由几何决定。例如离散最小可达曲率约为
`~2·corridor / (corridor² + edge²)`：若 `curvature_cap` 要求的最小转弯半径
超过 `corridor`，问题本身无解，服务会如实返回 `infeasible`（这正是
`examples/infeasible.json` 演示的）。

校验失败（点数越界、非法矩形、权重全 0、未知字段、非 JSON）返回 HTTP 422；
求解层失败仍返回 HTTP 200 + 失败 `status`，由响应体承载。

## 6. 夹具覆盖

- **窄通道** (`narrow_corridor.json`, `test_narrow_corridor_smooths_and_stays_clear`)：
  两侧墙形成 ~0.4 宽通道，平滑不能压向墙面。
- **障碍切角** (`corner_cut.json`, `test_corner_cut_does_not_take_illegal_chord`)：
  起点到终点的直线弦会穿过障碍；测试先断言该弦确实穿透，再断言优化解整段净空 ≥ clearance。
- **控制点之间藏碰撞** (`test_collision_hidden_between_control_points`)：
  顶点都在障碍外、连接段穿入——控制点检查会漏，密集/精确验证必须抓住。
- **重复点** (`repeated_points.json`)：连续重复点先折叠再优化、再按原索引展开，
  输出点数与输入一致；非连续的重访（闭合环）保留。
- **不同尺度** (`test_scale_invariance`, `large_scale_mm.json`)：
  ×1000 / ×0.001 缩放结果逐位一致；100 点毫米尺度夹具真实求解。
- **不可行 / 迭代预算 / 超时 / 输入即碰撞**：全部断言返回**原始路径**和对应失败状态。

### 梯度可信度

所有喂给 SLSQP 的解析梯度（目标函数、Menger 曲率、圆盘走廊、最小边长、
矩形 SDF）都用中心有限差分逐项核对（`tests/test_jacobians.py`，相对误差 < 1e-6）。
开发过程中这一步确实抓到过两个真实 bug：SDF 外侧/内部梯度方向错误，以及
约束值与雅可比缓存错配导致求解器误报成功、发布了违反曲率约束的路径。

## 7. 运行记录与完整性

- 每次调用把完整请求、响应和两个摘要写入 `./runs/<UTC时间戳>_<runid>.json`。
  可用环境变量 `TRAJ_SMOOTH_RUN_DIR=/some/dir` 改目录，置空字符串可关闭。
- `request_sha256` 是对**实际收到的原始 JSON** 规范化（键排序、紧凑分隔符）
  后的真实 SHA-256，可直接对发送字节复核；`response_sha256` 同理对响应体计算。
- NaN/±Infinity 诊断值在哈希和响应前被替换为 `null`，摘要基于严格合法 JSON。

## 8. 状态码语义

| `status` | 含义 |
|---|---|
| `optimal` | 收敛、所有硬约束满足、密集碰撞验证通过 |
| `infeasible` | 预算内找不到可行点 / 收敛点违反硬约束 |
| `iteration_limit` | 达到迭代预算但当前点仍近似可行（未充分收敛） |
| `timeout` | 超过 `timeout_seconds` 墙钟预算 |
| `input_collision` | 原始路径本身不满足净空（它是回退路径，必须先安全） |
| `solver_error` | 优化器抛异常 |
| `invalid_input` | 路径折叠后不足 2 点等 |
| `error` | 兜底 |
