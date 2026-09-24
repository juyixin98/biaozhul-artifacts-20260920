# 约束轨迹平滑（Constrained Trajectory Smoothing）

纯后端离线二维折线路径平滑服务。输入折线路径（首尾端点固定）、矩形障碍与安全余量，
在 **曲率上界** 与 **偏离原路径上界** 的硬约束下求解平滑折线；求解后对**整段轨迹的每一条线段**
做解析精确碰撞校验（不是只检查控制点）。不可行、未收敛或碰撞无法消除时，
**原样返回输入路径并给出失败状态，绝不发布非法新路径**。

技术栈：Python 3.12 · NumPy/SciPy（SLSQP）· FastAPI · Pydantic v2 · Pytest。

---

## 1. 数学模型

记输入折线点为 $p_0,\dots,p_{N-1}$，决策变量为内部控制点 $y_1,\dots,y_{N-2}$
（$y_0=p_0,\ y_{N-1}=p_{N-1}$ 固定）。为保证跨尺度数值稳定，坐标先除以特征尺度
$L$（路径包围盒对角线），在归一化空间内求解。

**目标函数**（权重可在 `app/optimizer.py` 调整，默认 `w_acc=1, w_jerk=1, w_track=0.5`）：

$$
\min_y\ \tfrac12 w_a\|D_2 y\|^2 + \tfrac12 w_j\|D_3 y\|^2
       + \tfrac12 w_t\|y-p\|^2 - w_t\,y^\top p
$$

- $D_2$：二阶差分（加速度 → 平滑度）；
- $D_3$：三阶差分（加加速度 → **限制曲率变化**）；
- 跟踪项：限制整体偏离原路径。

**硬约束**：

1. 每个内部控制点偏离量 $\|y_i-p_i\|\le d_{\max}$（`deviation_bound`）；
2. 每个三元组的 **Menger 曲率** $\kappa_i\le\kappa_{\max}$（`max_curvature`），
   约束写为 $\kappa_{\max}^2-\kappa_i^2\ge0$（平方避免不可导绝对值）；
3. 对每个邻近矩形障碍，沿线段自适应采样点的矩形有符号距离
   $\operatorname{sdf}(x)\ge s$（`safety_margin`）。障碍约束用**解析梯度**，
   曲率约束用前向差分 Jacobian。

求解器为 SciPy `scipy.optimize.minimize(method="SLSQP")`，目标值与梯度解析一致，
`ftol=1e-10`，主迭代预算默认 150、硬上限 500。

### 整段碰撞验证（关键安全环节）

优化中的障碍约束是离散采样，可能在采样间隙穿障。因此求解后用**解析方法**校验每条完整线段：

- Liang–Barsky 求线段与矩形的精确相交参数区间；
- 相交区间内 SDF 为分段凸二次函数，最小值只可能出现在端点、矩形中线、等距线根，
  全部解析枚举（`app/geometry.py: segment_rect_penetration`，实现已用 10 万点密集扫描交叉验证）；
- 任意线段侵入障碍或不满足安全余量即判失败；
- 失败一次时在**最深侵入参数点补采样**，以原解热启动重解一次；仍失败则回退原路径。

相切（仅接触矩形边界）间隙为 0，按不碰撞处理；需要安全余量时由 `safety_margin` 控制。

---

## 2. 目录结构

```
app/
  geometry.py    # 矩形 SDF+解析梯度、线段-矩形精确侵入、Menger 曲率、整段校验
  preprocess.py  # 连续重复点合并（保留非连续折返）、特征尺度
  optimizer.py   # 差分算子、自适应障碍采样、SLSQP 建模与求解
  smoother.py    # 服务管线：校验→预检→求解→精确验证→补采样重试→失败回退、落盘、SHA-256
  models.py      # Pydantic 请求模型
  main.py        # FastAPI 路由 /health /limits /smooth
run_smooth.py    # 离线 CLI
examples/        # 5 个夹具：窄通道、障碍切角、重复点、微观/宏观尺度
tests/           # 36 个自动化测试
runs/            # 每次求解的目标函数历史+约束残差（JSON，可关闭）
requirements.txt # 锁定依赖（pip freeze 全版本）
```

---

## 3. 本地启动

```bash
cd P067/b
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# HTTP 服务（纯后端，无前端页面）
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

接口：

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 健康检查 |
| GET | `/limits` | 点数与迭代预算上限 |
| POST | `/smooth` | 执行平滑，请求/响应见下 |

离线运行（不启动服务）：

```bash
python run_smooth.py examples/narrow_corridor.json
python run_smooth.py examples/angle_cutting.json --print-json
```

### 请求示例

```json
{
  "points": [[0, 0], [1, 0.05], [2, -0.05], [3, 0]],
  "obstacles": [[1.0, 0.4, 2.5, 4.0], [1.0, -4.0, 2.5, -0.4]],
  "deviation_bound": 0.25,
  "safety_margin": 0.02,
  "max_curvature": 2.5,
  "max_iter": 200,
  "save_run": true
}
```

| 字段 | 约束 | 含义 |
|---|---|---|
| `points` | 长度 2..100 的 `[x,y]` 列表，数值有限 | 折线，首尾为固定端点 |
| `obstacles` | `[xmin,ymin,xmax,ymax]`，严格正宽高 | 轴对齐矩形 |
| `deviation_bound` | >0，世界单位 | 控制点偏离原路径的硬上界 |
| `safety_margin` | ≥0 | 轨迹到障碍的最小间隙 |
| `max_curvature` | >0，1/世界单位 | Menger 曲率上界 |
| `max_iter` | 1..500，默认 150 | SLSQP 主迭代预算 |
| `save_run` | 默认 true | 目标函数/残差落盘到 `runs/` |

### 响应

成功：`success=true, result="smoothed"`，含 `metrics.objective_history`（目标函数历史）、
`metrics.residuals_norm/world`（偏离、曲率、障碍间隙约束残差）、`min_clearance_world`
（整段精确校验得到的最小间隙）。

失败：`success=false, result="original"`，`points` 与输入**逐点相同**，`reason` 取
`original_path_in_collision` / `solver_not_converged` / `collision_after_solve` /
`deviation_constraint_unsatisfied` / `curvature_constraint_unsatisfied` / `infeasible`。

每次响应附带 `integrity_sha256`：对规范 JSON 负载（sort_keys、紧凑分隔）的真实 SHA-256，
可用于核对结果未被篡改。

---

## 4. 验收命令

```bash
source .venv/bin/activate

# 1) 全部自动化测试（36 个，几何/服务/API 三层）
python -m pytest

# 2) 五个夹具逐个离线验收
for f in examples/*.json; do echo "== $f"; python run_smooth.py "$f"; done

# 3) HTTP 端到端验收（先启动 uvicorn，再执行）
uvicorn app.main:app --port 8000 &
curl -s http://127.0.0.1:8000/health
curl -s -X POST http://127.0.0.1:8000/smooth \
  -H 'content-type: application/json' --data @examples/narrow_corridor.json
```

测试覆盖要点：

- **窄通道**（两面长墙仅留窄缝）、**障碍切角**（L 形路径绕矩形内角）、
  **连续重复点**（合并求解、按原点序恢复）、**微观 1e-3 / 宏观 1e4 尺度**
  （验证归一化后跨尺度一致）；
- 控制点在障碍外、线段中点穿障时**整段校验必须检出**；解析侵入深度与 10 万点扫描对齐；
- SDF 解析梯度与中心差分对齐；曲率已知值（共线=0、单位圆=1）；
- 不可行（曲率上界极小）→ 原路径；原路径本身碰撞 → 原路径；
- 迭代预算被严格遵守；101 点、非法矩形、NaN、缺字段 → 422；
- 响应 SHA-256 真实可复算；目标函数历史与残差落盘；
- 解随输入变化（收紧偏离预算得到更小偏离），证明是真实优化而非固定平滑曲线。

---

## 5. 设计取舍与边界

- 不对输入做弧长重采样：点的数量与对应关系保持不变，重复点通过合并/恢复处理。
- 障碍约束是采样约束 + 解析全段验证的组合；采样密度随障碍最小尺寸自适应，
  漏检由补采样重试兜底，最多两次求解（预算合计不超过 `max_iter`）。
- 端点完全固定；若固定端点本身侵入障碍或不满足安全余量，直接判失败（平滑无法修复）。
- 单次请求规模上限 100 点 / 500 次主迭代；无认证，仅用于本地/离线场景。
