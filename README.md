# 点云刚体配准后端（Point-to-Point ICP）

使用 **Python + FastAPI + NumPy + SciPy** 实现的点到点迭代最近点（ICP）刚体配准服务。
**仅使用合成数据 / 离线回放**，不接真实硬件，不含可视化。

## 功能

- 点到点 ICP 主循环：最近邻（`scipy.spatial.cKDTree`）→ 离群截断 → SVD 刚体估计。
- SVD 刚体解（Arun/Horn）：**拒绝反射解**，强制 `det(R)=+1`；秩亏（共线/平面）几何做专门处理并报告退化。
- 离群截断两种手段可组合：
  - 硬距离阈值 `max_correspondence_distance`；
  - 稳健分位截断 `robust_quantile`（每轮只保留最近的若干比例配对，适合局部重叠）。
- 如实报告：`converged` / `max_iterations`（不收敛）/ `insufficient_pairs`（有效配对不足）。
- 退化检测：依据交叉协方差矩阵奇异值，区分 **共线（rank=1）** 与 **平面（rank=2）**。
- **不把局部最优宣称成全局最优**：ICP 只是局部优化器，所有成功结果都带局部性/可能局部最优的告警。

## 目录结构

```
.
├── app/
│   ├── icp_core.py      # ICP 算法核心（纯 NumPy/SciPy）
│   ├── synthetic.py     # 合成点云生成（带真值，便于误差评估）
│   ├── schemas.py       # Pydantic 请求/响应模型
│   └── main.py          # FastAPI 应用与路由
├── scripts/
│   └── demo.py          # 离线 / HTTP 示例脚本
├── tests/
│   ├── test_icp.py      # 算法层测试
│   └── test_api.py      # HTTP 层测试
├── requirements.in      # 顶层依赖（不锁版本）
├── requirements.txt     # 锁定依赖（pip freeze 完整解析结果）
└── pyproject.toml
```

## 环境与安装

- Python 3.10+（开发实测 3.12）

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt      # 安装锁定依赖
```

> `requirements.txt` 同时含运行时与测试依赖（pytest、httpx）。仅运行服务时可用
> `pip install -r requirements.in` 获取未锁定的最小集合。

## 启动命令

### 1. 启动 HTTP 服务

```bash
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

接口：

| 方法 | 路径 | 说明 |
|------|------|------|
| GET  | `/health` | 健康检查 |
| POST | `/api/icp` | 对两组三维点做 ICP 配准 |
| POST | `/api/synthetic-scene` | 生成带真值的合成变换点云 |
| POST | `/api/demo` | 运行内置验收场景 |

`POST /api/icp` 请求体（节选）：

```json
{
  "source": [[x,y,z], ...],
  "target": [[x,y,z], ...],
  "R0": [[...3x3...]], "t0": [x,y,z],
  "max_iterations": 50,
  "tolerance": 1e-7,
  "max_correspondence_distance": null,
  "robust_quantile": 0.9,
  "min_inliers": 3,
  "degeneracy_ratio": 1e-3
}
```

非正交或 `det≠+1` 的 `R0`、非三维点等非法输入返回 `422`。

### 2. 离线示例（无需服务）

```bash
python -m scripts.demo --scenario nominal           # 全重叠+噪声
python -m scripts.demo --scenario collinear         # 共线退化
python -m scripts.demo --scenario partial_overlap   # 60%重叠+杂点
python -m scripts.demo --scenario bad_initial       # 错误初值→局部最优
python -m scripts.demo --scenario nonconverge       # 预算不足→不收敛
```

### 3. 通过 HTTP 调用示例（需先启动服务）

```bash
python -m scripts.demo --url http://127.0.0.1:8000 --scenario nominal
```

### 4. 运行测试

```bash
python -m pytest
```

## 验收方法与实测结果

以下结果在本机（Python 3.12，NumPy 2.5.3 / SciPy 1.18.1）**实际运行**得到。

### 测试套件

```
29 passed, 1 warning in 0.86s
```

覆盖：已知变换+噪声精度、完美初值、反射解拒绝、共线/平面退化、
局部重叠+杂点、错误初值（不宣称全局最优）、不收敛、有效配对不足、
输入校验、RMSE 单调性、确定性，以及 5 个场景的 HTTP 端到端测试。

### 示例场景实测（`python -m scripts.demo`，seed=0）

| 场景 | status | 迭代 | RMSE | 旋转误差 | 平移误差 | 说明 |
|------|--------|------|------|----------|----------|------|
| nominal（20°/0.5，σ=0.01） | converged | 10 | 0.0159 | 0.074° | 0.0029 | 精确收敛到噪声水平 |
| collinear（共线，真值初值） | converged | 2 | 0.0 | 0.0° | 0.0 | 直线精确对齐，报告 collinear 退化 |
| partial_overlap（60%重叠+60杂点） | converged | 13 | 0.0523 | 0.289° | 0.0060 | 分位截断剔除杂点后恢复真值 |
| bad_initial（初值偏差~140°） | converged | 13 | 0.299 | 122.6° | 0.015 | **陷入错误局部最优，显式告警** |
| nonconverge（仅给2轮） | max_iterations | 2 | 0.406 | 62.1° | 0.712 | 如实报告不收敛 |

### 关于局部最优（重要）

`bad_initial` 场景中 ICP 形式上“收敛”了，但旋转误差高达 **122.6°**。
系统不会把它当成功：当 RMSE 超过目标点云平均最近邻间距的一半时，
返回 `likely a local minimum ... NOT verified as the global optimum` 告警；
即使残差很小，也统一附带 `global optimality is not guaranteed and not asserted`。

### 关于共线点云的固有局限（重要）

纯几何最近邻在**共线**点云上存在配对歧义：从偏差较大的初值出发，线上各点彼此难辨，
最近邻配对会错乱，ICP 会爬向错误局部解（实测即使只有 5° 偏差，单位初值下约 98%
配对错误）。这是 ICP 本身的局限而非实现缺陷。本实现：

- 真值/良好初值（配对正确）下：直线方向与平移精确恢复（RMSE=0），同时报告 `collinear` 退化；
- 较差初值下：返回错误局部解并保留退化告警，不宣称成功；
- SVD 在秩 1 时标准“反射翻转修正”会产生沿线 180° 翻转（虽为合法刚体旋转但颠倒点序），
  代码改为返回对齐两直线方向的**最小旋转角**解，并明确说明绕轴旋转不可观。

平面（rank=2）情形：平面内法向轴的旋转仅由面内协方差形状决定，代码标记 `planar` 告警。

## 设计说明 / 边界

- 坐标系：`R0/t0` 与返回的 `R/t` 均表示 **source → target**，即 `target ≈ R·source + t`。
- 退化阈值 `degeneracy_ratio`（默认 1e-3）：奇异值 / 最大奇异值低于它即判秩亏。
- 共线/平面判定基于中心化点云交叉协方差的 SVD，对精确秩亏稳健；带噪近平面（满秩但
  薄）不会被误判为退化。
- 每轮 RMSE 统计的是当轮**内点**配对（截断后）的均方根。
- 纯 CPU、确定性算法（无随机初始化），相同输入结果一致（有专门测试）。

## 未完成项 / 已知限制（如实记录）

1. **不做全局配准**：没有 RANSAC / FGR / 分支定界等全局初始化；初值不好时可能局部最优，
   仅做检测与告警，不自动重启动。
2. **无点到面 ICP、无尺度估计、无权重/颜色/法向特征**：仅等权点到点刚体（6 自由度）。
3. **共线点云对初值敏感**（见上）：当前不提供“沿线搜索配对”的专门策略。
4. **未做性能优化**：KDTree 每轮重建在 target 固定时其实可复用（target 不变，已构建一次；
   但未对超大点云做向量化批处理/并行或采样加速），未做百万级点云压测。
5. **无鉴权/限流/持久化/容器化**：仅本地/内网教学与验收用途。
6. **依赖版本偏新**（NumPy 2.x）：锁定文件已固定可用版本；若需 NumPy 1.x 兼容需另行验证。
7. `Starlette TestClient` 对 `httpx` 有一条弃用告警（不影响功能）。
