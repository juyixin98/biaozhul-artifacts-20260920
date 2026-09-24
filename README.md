# 多目标轨迹关联（Multi-Object Track Association）

二维检测流的**纯后端**多目标跟踪服务：匀速模型 Kalman 滤波预测 +
Mahalanobis 统计门控 + 匈牙利全局最优分配。Python / NumPy / SciPy / FastAPI。

- 状态向量 `[x, y, vx, vy]`，观测 `[x, y]`；时间间隔 **dt 真实参与**
  状态转移 `F(dt)` 与过程噪声 `Q(dt)`（白加速度模型，含 `dt^3/dt^2/dt` 项）
- 新轨迹为 `tentative`，**连续命中** `confirm_hits` 帧后确认；
  连续丢失超过 `max_misses` 帧删除；滑行期间持续输出预测
- 帧内重复检测用并查集按距离**聚合**；帧消息重复投递**幂等**，ID 不增长
- `frame_id` / `timestamp` 必须严格递增，**乱序帧明确拒绝**（422 / 409）
- 每次关联返回：每条轨迹的**预测位置**、**欧氏距离**、**平方 Mahalanobis
  距离**、门控结果、是否选中，以及全局 `selection_basis`
- 响应体用 **HMAC-SHA256 真实签名**（每会话 CSPRNG 随机密钥），
  请求体用 **SHA-256** 指纹做幂等去重（均为标准库真实密码学实现）
- 评测夹具：交叉运动 / 短时遮挡 / 重复检测 / 空帧，量化 **ID 切换数**与
  **位置误差（RMSE / 平均 / 最大）**；**真值 ID 仅用于离线打分，
  关联代码路径不读取任何真值**

## 目录结构

```
app/
  kalman.py       # 匀速 2D Kalman 滤波器（F(dt)/Q(dt)/predict/update/门控距离）
  tracker.py      # 轨迹生命周期、门控代价矩阵、scipy 匈牙利分配、聚合、时序拒绝
  schemas.py      # Pydantic 协议模型（禁 NaN/Inf、禁多余字段）
  crypto.py       # SHA-256 指纹、HMAC-SHA256 签名/验签（hmac.compare_digest）
  storage.py      # 内存会话存储、帧级幂等缓存、签名注入
  evaluation.py   # 夹具生成与离线指标（真值只存在于本模块）
  main.py         # FastAPI 路由
scripts/
  generate_examples.py  # 生成 examples/*.json（协议数据，不含真值）
  run_evaluation.py     # 离线验收 CLI（表格 / JSON）
  replay_client.py      # 标准库 urllib 真实 HTTP 回放 + 验签 + 幂等/乱序探测
tests/                # 49 个 pytest 用例
examples/             # 示例会话与帧输入
requirements.txt      # 直接依赖
requirements.lock     # pip freeze 完整锁定
```

## 本地启动

需要 Python 3.12（其它 3.10+ 也可）。

```bash
cd /home/admin/Downloads/biaozhul/P053/a
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.lock          # 或 pip install -r requirements.txt
python scripts/generate_examples.py       # 生成示例输入（已附带，可重复生成）

uvicorn app.main:app --host 127.0.0.1 --port 8000
# 交互式 API 文档： http://127.0.0.1:8000/docs
```

## 验收命令

```bash
# 1) 单元 + 协议 + 夹具自动化测试（49 项）
. .venv/bin/activate
python -m pytest -q

# 2) 离线夹具验收（ID 切换 / RMSE 表；退出码非 0 即不达标）
python scripts/run_evaluation.py
python scripts/run_evaluation.py --noisy --seed 7   # 加高斯噪声的鲁棒性
python scripts/run_evaluation.py --json             # 机器可读

# 3) 真实 HTTP 端到端（先在另一个终端启动 uvicorn）
uvicorn app.main:app --port 8000 &
python scripts/replay_client.py examples/frames_duplicates.json --base-url http://127.0.0.1:8000
```

### 当前实测结果（确定性夹具）

| scenario         | ID切换 |   RMSE | 平均误差 | 最大误差 | 确认ID | 新生 | 删除 |
|------------------|-------:|-------:|---------:|---------:|-------:|-----:|-----:|
| crossing         |      0 | 0.0026 |   0.0009 |   0.0104 | [1, 2] |    2 |    0 |
| occlusion_gap4   |      0 | 0.0015 |   0.0005 |   0.0059 |    [1] |    1 |    0 |
| occlusion_gap6   |      1 | 0.0024 |   0.0013 |   0.0059 | [1, 2] |    2 |    1 |
| duplicates       |      0 | 0.0247 |   0.0247 |   0.0251 |    [1] |    1 |    0 |
| empty_frames     |      0 | 0.0013 |   0.0005 |   0.0044 |    [1] |    1 |    0 |

- `crossing`：两目标错时交叉，**0 次 ID 切换**；
- `occlusion_gap4`：遮挡短于 `max_misses=5`，滑行后身份保持（0 切换）；
- `occlusion_gap6`：遮挡超过阈值，旧轨迹删除、新轨迹确认，离线计 1 次切换
  （这是期望行为，验收脚本不检查该场景 0 切换）；
- `duplicates`：每帧两个近点被聚合，始终只有 1 条轨迹；
- `empty_frames`：出生前/运行中空帧不产生虚假轨迹。

## HTTP 协议

### `GET /health`
返回 `{"status":"ok","sessions":N}`。

### `POST /sessions` → 201
```json
{
  "session_id": "demo",
  "config": {
    "confirm_hits": 3,
    "max_misses": 5,
    "gate_threshold": 9.210,
    "merge_radius": 0.25,
    "process_noise": 1.0,
    "measurement_var": 1.0
  }
}
```
响应包含完整配置与会话 **`signing_key_hex`（仅本次返回一次）**。
配置全部可省略；`gate_threshold` 默认 9.210（自由度 2 的 χ² 99% 分位）。

### `POST /sessions/{id}/frames`
请求：
```json
{
  "frame_id": 7,
  "timestamp": 7.0,
  "detections": [
    {"x": 5.62, "y": 0.01, "detection_id": "A-7"}
  ]
}
```
响应（节选）：
```json
{
  "frame_id": 7, "timestamp": 7.0, "dt": 1.0,
  "detections": [{"index": 0, "x": 5.62, "y": 0.01, "member_ids": ["A-7"]}],
  "merged_groups": [["A-7"]],
  "tracks": [{
    "track_id": 1, "state": "confirmed",
    "hits": 6, "hit_streak": 6, "misses": 0,
    "position": [5.61, 0.02], "velocity": [0.80, 0.00],
    "position_variance": [0.12, 0.12],
    "associated_detection_index": 0, "age_frames": 7
  }],
  "assignments": [{
    "track_id": 1, "detection_index": 0,
    "predicted_xy": [5.60, 0.00], "measurement_xy": [5.62, 0.01],
    "euclidean_distance": 0.022,
    "mahalanobis_sq": 0.004,
    "gate_threshold": 9.21,
    "within_gate": true, "selected": true
  }],
  "rejected_by_gate": [],
  "births": [], "matched": [[1, 0]], "coasted": [], "deleted": [],
  "next_track_id": 2,
  "selection_basis": {
    "method": "global_optimal_hungarian",
    "cost": "mahalanobis_sq",
    "gate": "chi_squared_2dof",
    "gate_threshold": 9.21,
    "assignment_rule": "scipy.optimize.linear_sum_assignment on gate-allowed costs; one-to-one; ...",
    "state_transition": "constant_velocity, dt=1.0"
  },
  "replay": false,
  "signature_algorithm": "HMAC-SHA256(canonical_json(body_without_signature))",
  "signature": "<64 hex chars>"
}
```

### 时序与幂等语义

| 情况 | 状态码 | 行为 |
|---|---|---|
| 正常新帧 | 200 | 推进跟踪器 |
| 相同 `frame_id` + **相同**请求体（重复投递） | 200 | `replay=true`，返回缓存响应，**状态不前进、ID 不增长** |
| 相同 `frame_id` + **不同**请求体 | 409 | `frame_payload_conflict`，拒绝覆盖 |
| `frame_id` 回退 | 409/422 | 已存在同号按冲突处理；其余按乱序拒绝 |
| `timestamp` 回退 / 同时间戳第二帧 | 422 | `stale_frame`，状态不改变 |
| NaN/Infinity、多余字段 | 422 | Pydantic 校验拒绝 |

幂等指纹：`SHA256(canonical_json(request))`，键排序、紧凑分隔。

### 签名验签

对响应去掉 `signature` 字段后的对象做规范化 JSON，计算
`HMAC-SHA256(key, canonical)`，与响应 `signature` 常量时间比较。
`tests/test_api.py` 与 `scripts/replay_client.py` 都做了真实验签。

## 算法说明

1. **预测**：每条存活轨迹 `x^- = F(dt)x`，`P^- = F(dt)P F(dt)^T + Q(dt)`，
   `F = [[I, dt I],[0,I]]`；`Q` 取连续白加速度离散化
   `q[[dt^3/3, dt^2/2],[dt^2/2, dt]]`。
2. **门控代价**：对每对 (轨迹 i, 检测 j) 用预测位置协方差
   `S = H P^- H^T + R` 算新息 `ν = z - H x^-` 与
   `d² = ν^T S⁻¹ ν`；`d² > gate_threshold` 的候选置为禁止（inf）。
   门限随 `P^-` 自适应放大，因此长间隔滑行后门会自然放宽以便重获。
3. **分配**：在允许候选上跑 `scipy.optimize.linear_sum_assignment`
   （匈牙利/JVCR），全局一一最小化总 `d²`；`selected=true` 即最终选择依据。
4. **更新/滑行/新生**：匹配上的做 Kalman 量测更新并累计连续命中；
   未匹配轨迹丢测 +1、进入/保持滑行，超阈值删除；未匹配检测生成
   `tentative` 新轨迹。
5. **聚合**：帧内欧氏距离 ≤ `merge_radius` 的检测用并查集合并，均值作为量测。

## 真值隔离声明

`app/evaluation.py` 中的 `truth_id`（"A"/"B"）只出现在离线打分代码里；
提交给 `Tracker.step` 的只有 `(x, y, detection_id)`，而 `detection_id`
仅是观测端审计标签，关联器从不读取它参与匹配（见
`tests/test_evaluation.py::test_tracker_never_receives_truth_ids`）。
