# 轨迹误差评估 API（Trajectory Error Evaluation）

纯后端离线轨迹评估服务：输入**估计轨迹**与**真值轨迹**，按限定时间差匹配，
做刚体（SE(3)，可选相似 Sim(3) 且单独标记）对齐，计算 **ATE** 与指定
时间跨度的 **RPE**。旋转误差采用 SO(3) 上的测地线角距离，天然跨越
±π/2π 边界。重复时间戳、零匹配、退化对齐一律明确报错，**绝不补零**。

技术栈：Python 3.10+ · NumPy · FastAPI · Pydantic v2 · pytest。

---

## 1. 目录结构

```
app/
  errors.py        # 领域错误：重复时间/零匹配/退化/无效姿态/非法请求
  rotation.py      # 四元数 [w,x,y,z]、旋转矩阵、SO(3) 测地线角距离
  trajectory.py    # 轨迹校验（重复时间戳等）+ 最近邻时间匹配
  alignment.py     # Umeyama 刚体/相似变换对齐（含退化检测）
  metrics.py       # ATE 与固定时间跨度 RPE
  evaluation.py    # 端到端流水线 + 结果序列化（尺度结果单独标记）
  schemas.py       # 请求/响应 Pydantic 模型
  main.py          # FastAPI 应用：GET /health, POST /evaluate
tests/             # 27 项自动化测试（HTTP 层 + 数值原语）
examples/          # 6 个示例请求（成功 4 个 + 错误 2 个）
scripts/generate_examples.py
requirements.txt / requirements-lock.txt
```

## 2. 本地启动

```bash
cd /home/admin/Downloads/biaozhul/P059/a
python3 -m venv .venv
.venv/bin/pip install -r requirements-lock.txt   # 或 -r requirements.txt
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000
```

健康检查：

```bash
curl -s http://127.0.0.1:8000/health
# {"status":"ok"}
```

交互式文档：<http://127.0.0.1:8000/docs>。

## 3. 验收命令

一键运行全部自动化测试（27 项，含纯平移、尺度漂移、缺测、旋转跨界、
三类硬错误与数值精度校验）：

```bash
.venv/bin/python -m pytest tests/ -v
```

用真实 HTTP 请求验收示例数据（服务已启动时）：

```bash
# 纯刚体平移+旋转：ATE 平移 RMSE 应为 ~0
curl -s -X POST http://127.0.0.1:8000/evaluate \
  -H 'Content-Type: application/json' \
  -d @examples/rigid_shift.json | python3 -m json.tool

# 尺度漂移（similarity 模式）：返回 fitted_sim3 尺度并单独标记
curl -s -X POST http://127.0.0.1:8000/evaluate \
  -H 'Content-Type: application/json' \
  -d @examples/scale_drift.json | python3 -m json.tool

# 缺测：coverage 部分覆盖，ATE 帧数 = 实际匹配数，无补零
curl -s -X POST http://127.0.0.1:8000/evaluate \
  -H 'Content-Type: application/json' \
  -d @examples/missing_frames.json | python3 -m json.tool

# 旋转跨界：±π 附近的姿态按真实 ~7.4° 误差计算（不是 ~352°）
curl -s -X POST http://127.0.0.1:8000/evaluate \
  -H 'Content-Type: application/json' \
  -d @examples/rotation_branch_cut.json | python3 -m json.tool

# 错误：重复时间戳 -> HTTP 400 code=duplicate_timestamp
curl -s -X POST http://127.0.0.1:8000/evaluate \
  -H 'Content-Type: application/json' \
  -d @examples/error_duplicate_ts.json

# 错误：零匹配 -> HTTP 400 code=zero_matches
curl -s -X POST http://127.0.0.1:8000/evaluate \
  -H 'Content-Type: application/json' \
  -d @examples/error_zero_matches.json
```

示例文件可通过 `.venv/bin/python scripts/generate_examples.py` 重新生成。

## 4. 请求协议 `POST /evaluate`

```json
{
  "estimated": [
    {"timestamp": 0.0,
     "position": [x, y, z],
     "orientation": [w, x, y, z]}
  ],
  "ground_truth": [ /* 同构，时间戳单位秒 */ ],
  "max_time_diff": 0.02,
  "align_mode": "rigid",
  "rpe": [{"delta": 2.0, "tolerance": 0.05}]
}
```

| 字段 | 说明 |
|---|---|
| `max_time_diff` | **匹配门限（秒，≥0）**。每个估计时刻找最近 GT 时刻，`|dt|` 超门限即不匹配；不做插值、不补零 |
| `align_mode` | `rigid`（默认，SE(3)，保持真值尺度）或 `similarity`（Sim(3)，拟合尺度） |
| `rpe` | 0..N 个固定时间跨度：`delta` 秒（>0）与允许偏差 `tolerance`（≥0） |
| `orientation` | `[w,x,y,z]` 四元数，无需预归一化；零模四元数报 `invalid_orientation` |

### 响应要点

- `match_count`、`coverage.estimated`、`coverage.ground_truth`：
  匹配条数与双侧覆盖率（GT 侧按实际被用到的不同样本计）；
  `time_errors` 给出每条匹配的真实时间残差，`max_abs_time_error` 为最大值。
- `alignment`：`rotation_matrix` / `rotation_quat_wxyz` / `translation` 与
  变换公式 `p_aligned = scale * R @ p_estimated + translation`。
  - **尺度单独标记、绝不混淆**：
    rigid → `"scale": {"mode": "fixed_rigid", "value": 1.0}`；
    similarity → `"scale": {"mode": "fitted_sim3", "value": s,
    "scale_drift_abs": |s-1|, "warning": ...}`。
- `ate`：每帧 `trans_error`（米）与 `rot_error_rad/deg`，及
  `rmse/mean/median/std/max`。
- `rpe`：每个跨度返回 `num_pairs`、每对的真实间隔 `actual_span` 与
  平移/旋转误差；跨度内缺测导致无法成 pair 时 `num_pairs=0`、
  统计字段为 `null`（不伪造零误差）。

### 错误响应（HTTP 400）

```json
{"error": true, "code": "duplicate_timestamp", "message": "..."}
```

| code | 触发条件 |
|---|---|
| `duplicate_timestamp` | 任一轨迹内部存在重复时间戳（排序后仍非严格递增） |
| `zero_matches` | 门限内一对匹配都没有 |
| `degenerate_alignment` | 匹配点 < 2、全重合（尺度/旋转不可辨识）、共线（绕轴旋转不可观）或拟合尺度非正 |
| `invalid_orientation` | 零模四元数 |
| `invalid_request` | 空轨迹、非有限数值、负的门限/容差等 |

请求结构本身的校验错误由 FastAPI/Pydantic 返回 HTTP 422。

## 5. 计算口径

1. **匹配**：输入先按时间排序（顺序无关），最近邻 + 硬门限；等距取更早 GT，
   结果确定可复现。未匹配样本只体现在覆盖率中。
2. **对齐（Umeyama 1991）**：在匹配点上最小化均方位置残差。旋转用
   Wahba 闭式解（Davenport K 矩阵最大特征向量，带反射保护）；平移由
   质心闭式得到；相似模式的尺度
   `s = tr(Rᵀ Σ_qp) / σ_p²`，方向为**估计→真值**（估计被放大 1.2× 时
   `s≈0.8333`）。
3. **ATE**：对齐后逐帧欧氏位置误差与姿态误差。
4. **RPE**：在已对齐、已匹配的序列上，为每个起点选取时间间隔最接近
   `delta` 且落在 `tolerance` 内的终点（缺测自然少成 pair）；
   相对运动 `p_rel = R_iᵀ(p_j−p_i)`、`R_rel = R_iᵀR_j`，
   平移误差为欧氏距离，旋转误差为测地线角距离。
5. **旋转角距离**：
   `angle(R₁,R₂) = arccos(clamp((tr(R₁ᵀR₂)−1)/2, −1, 1)) ∈ [0, π]`，
   SO(3) 上的真实度量，连续、无奇异、无跨界跳变。

## 6. 设计约束的落实

- 不插值、不外推、不补零：缺测 → 覆盖率下降；无法成 RPE pair →
  `num_pairs=0` 且统计为 `null`。
- 退化即报错：重合/共线几何无法确定旋转或尺度，绝不返回任意解。
- 尺度对齐必须显式选择，返回中以 `mode` 与警告字段与刚体结果隔离，
  两种模式的数字不合并、不可直接比较。
- 纯计算模块（`app/*.py`）与 Web 层解耦，可在脚本中直接 `from app.evaluation import evaluate`。
