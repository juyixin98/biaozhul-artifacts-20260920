# 速度约束路径参数化（离线 TOPP）

仅使用合成数据 / 离线回放，不连接任何真实硬件，不包含任何可视化。

核心算法：给定弧长采样 `s`、曲率 `kappa`、速度上限 `v_max`、纵向加速度区间
`[a_min, a_max]`、横向加速度上限 `a_lat_max` 以及首尾速度 `v_start / v_end`，
用**前向 + 后向扫描**生成可行速度包络，再按“段内匀加速”模型积分得到各段时间与
节点时刻。

## 目录结构

```
speed_profile/          核心库（纯 Python，仅依赖 numpy / scipy）
  model.py              数据结构与输入校验
  topp.py               前后向扫描、时间积分、约束复核
  scenarios.py          合成场景：直线 / 急弯（半圆发卡弯）/ S 形缓弯
  analytic.py           解析匀加速案例（用于误差核对）
  api.py                FastAPI 应用（离线计算服务，无硬件、无可视化）
examples/run_demo.py    离线回放示例：运行全部场景并打印核对结果
tests/                  pytest 自动化测试
requirements.in         直接依赖（版本范围）
requirements.txt        锁定依赖（pip freeze 精确版本）
```

## 运行环境与依赖

- Python 3.12（3.10+ 应可运行，锁定文件在 3.12 下生成）
- 依赖安装（建议虚拟环境）：

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

直接依赖见 `requirements.in`；`requirements.txt` 为完整锁定版本。

## 启动命令

### 1. 自动化测试

```bash
pytest -q
```

### 2. 离线回放示例（直线 / 急弯 / 首尾静止 / 零长度段 / 解析匀加速误差）

```bash
python examples/run_demo.py
```

### 3. 启动 FastAPI 服务（纯离线计算）

```bash
uvicorn speed_profile.api:app --host 127.0.0.1 --port 8000
```

调用示例：

```bash
curl -s http://127.0.0.1:8000/health
curl -s http://127.0.0.1:8000/api/scenarios
curl -s -X POST http://127.0.0.1:8000/api/parameterize \
  -H 'Content-Type: application/json' \
  -d '{"s":[0,1,2,3],"kappa":[0,0,0,0],"v_max":5.0,
       "a_max":3.0,"a_min":-3.0,"v_start":0,"v_end":0}'
```

也可以先 `GET /api/scenarios/hairpin` 取场景输入再 POST 回放。

## 算法说明

离散网格上每个节点 i 有位置 s_i、曲率 κ_i。约束：

1. 速度上限：`v_i <= v_cap_i = min(v_max_i, sqrt(a_lat_max / |κ_i|))`，
   并在首节点并入 v_start、尾节点并入 v_end；κ=0 处无横向速度限制。
2. 纵向加速度（段内视为匀加速）：
   `a_min <= (v_{i+1}^2 - v_i^2) / (2 Δs) <= a_max`。

- **前向扫描**：`v_f[i] = min(v_cap[i], sqrt(v_f[i-1]^2 + 2 a_max Δs))`；
- **后向扫描**：`v_b[i] = min(v_cap[i], sqrt(v_b[i+1]^2 + 2 (-a_min) Δs))`；
- **包络**：`v = min(v_f, v_b)`；
- 段时间（恒加速度解析解）：`Δt = 2 Δs / (v_i + v_{i+1})`，节点时刻累加；
- 用 PCHIP 光滑插值速度 + `scipy.integrate.quad` 自适应积分独立复核总时间；
- 逐段复核速度 / 加速度约束，并检测首尾不可达、正长度段上速度为 0 等不可行情况。

### 零长度段处理

允许 `s` 中存在相邻重复点（Δs=0）。扫描时该边的可达速度退化为
`sqrt(v^2 + 0) = v`，两个方向的扫描 + 取 min 会把同一物理位置上所有重复节点的
最严速度约束统一起来；该边段时间记 0、段加速度记 NaN（不参与加速度复核）。
若出现 s 倒退或数组长度不一致等输入错误，直接抛出 `ValueError`。

## 实测结果

见 [RUN_LOG.md](RUN_LOG.md)，记录了在本机实际运行 `pytest` 与示例的完整输出、
解析匀加速案例误差，以及未完成项 / 已知限制。
