# 多目标轨迹关联后端（Multi-Object Tracking API）

纯后端的二维检测流多目标跟踪服务：匀速 Kalman 预测 → 马氏距离门控 →
匈牙利一对一分配。基于 **Python 3.12 / NumPy / SciPy / FastAPI**，无前端。

## 算法与协议要点

* **状态模型**：`x = [px, py, vx, vy]ᵀ`，量测为 `[px, py]ᵀ`。状态转移矩阵
  `F(dt)` 与过程噪声 `Q(dt)` 都显式依赖两帧之间的真实时间间隔 `dt`
  （连续白噪声加速度模型，位置方差随 `dt³/3` 增长）。
* **关联**：对每条存活轨迹计算量测的**平方马氏距离** `yᵀS⁻¹y`，先过
  χ²(2) 门控（默认 99% 分位，可配固定阈值），再对可行代价矩阵用
  `scipy.optimize.linear_sum_assignment` 做全局一对一最优分配。
* **轨迹生命周期**：
  * 新轨迹为 `tentative`，需**连续命中** `hits_to_confirm`（默认 3）次才
    `confirmed`；中间漏检会清零连续命中计数。
  * 确认轨迹连续丢失 `max_misses`（默认 4）帧后删除；未确认轨迹阈值为
    `tentative_max_misses`（默认 2）。ID 单调递增、删除后不复用。
* **重复消息不增加 ID**：
  * 帧内重复检测（欧氏距离 ≤ `duplicate_eps`）先做单链聚类融合为质心，
    被丢弃的副本在响应 `duplicate_detections` 中说明；
  * 同一 `frame_id` 的 HTTP 重放（消息重复投递）直接返回首次响应，
    幂等且不新建轨迹；若重放帧体不同则返回 `409 replay_body_mismatch`。
* **乱序明确拒绝**：`frame_id` 必须严格递增，`timestamp` 必须严格增大；
  违反时返回 `409 frame_out_of_order`，错误信息说明原因。
* **每次关联都输出依据**：预测位置、量测、平方马氏距离、欧氏距离、门限、
  是否在门内、次优候选（runner-up）以及选择依据
  （匈牙利全局最小、一对一、门控可行）。

## 真值防火墙

夹具文件中可以带 `object_id` 真值，但**只有离线评价器读它**。送入跟踪器的
`Detection` 只有 `x / y / label`（`label` 是不透明的检测器行标签，仅用于
重复诊断回显）；`tests/test_scenarios.py::test_tracker_never_receives_object_id`
静态+边界双重保证关联代码无法接触真值 ID。

## 目录结构

```
app/
  mot/kalman.py     # 匀速 Kalman 滤波（F(dt)、Q(dt)、Joseph 协方差更新）
  mot/tracker.py    # 多目标跟踪器：门控、匈牙利、确认/删除、重复融合、乱序拒绝
  schemas.py        # Pydantic 线协议
  security.py       # secrets 密钥、HMAC-SHA256 签名/验签（compare_digest）、SHA-256
  service.py        # 会话存储、帧重放幂等/冲突、结果序列化
  main.py           # FastAPI 路由
scripts/
  make_fixtures.py  # 确定性生成 4 个评价夹具（含真值）
  evaluate.py       # 离线指标：ID 切换、位置误差、轨迹数、删除数
examples/
  client_demo.py    # 签名客户端：正常流 + 重放 + 乱序 + 伪造签名
  load_sample.py    # 把 sample_frames.json 逐帧推给服务
  sample_frames.json# 示例输入（含重复检测帧与空帧）
fixtures/*.json     # 交叉/遮挡/重复/空帧四个场景
tests/              # 36 个自动化测试（单元 + 夹具验收 + HTTP 端到端）
requirements.txt    # 锁定依赖（pip freeze 全量版本）
```

## 本地启动

```bash
cd b
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt          # 使用锁定版本

uvicorn app.main:app --host 127.0.0.1 --port 8000
```

（若 8000 被占用，换端口并在客户端设置 `MOT_BASE_URL=http://127.0.0.1:<port>`。）

## HTTP 协议（签名真实执行）

| 方法 | 路径 | 鉴权 |
|---|---|---|
| GET  | `/health` | 无 |
| POST | `/sessions` | 无；返回一次性 `secret` |
| POST | `/sessions/{id}/frames` | HMAC-SHA256 |
| GET  | `/sessions/{id}` | HMAC-SHA256 |

签名串（换行分隔）：

```
<METHOD>\n<path>\n<unix秒时间戳>\n<body的SHA-256十六进制>
```

请求头：`X-Session-Id`、`X-Timestamp`（与服务端偏差超过 300 秒拒绝，防重放）、
`X-Signature`（HMAC-SHA256 十六进制摘要，使用 `hmac.compare_digest` 常量时间比较）。

帧请求体：

```json
{
  "frame_id": 3,
  "timestamp": 0.3,
  "detections": [
    {"x": 1.5, "y": 0.0, "label": "A3"},
    {"x": 8.5, "y": 0.0, "label": "B3"}
  ]
}
```

帧响应（节选；完整字段见 `app/schemas.py`）：

```json
{
  "frame_id": 3, "timestamp": 0.3, "dt": 0.1, "gate_threshold": 9.210,
  "associations": [{
    "track_id": 1, "detection_index": 0,
    "prediction": [1.0, 0.0], "measurement": [1.5, 0.0],
    "mahalanobis_sq": 0.01, "euclidean": 0.008,
    "in_gate": true,
    "runner_up": {"detection_index": 1, "mahalanobis_sq": 120.4, "euclidean": 7.1},
    "selection_basis": "hungarian-global-minimum: ..."
  }],
  "unmatched_tracks": [], "unmatched_detections": [],
  "new_tracks": [], "confirmed_tracks": [], "deleted_tracks": [],
  "duplicate_detections": [], "cost_matrix": [[0.01, 120.4]], "rows_track_ids": [1]
}
```

错误体形如 `{"detail": {"code": "frame_out_of_order", "message": "..."}}`，
涵盖 `missing_credentials / bad_timestamp / timestamp_skew / bad_signature /
session_not_found / session_mismatch / replay_body_mismatch /
frame_out_of_order`。

## 验收命令

```bash
source .venv/bin/activate

# 1) 全量自动化测试（36 个：Kalman、跟踪器、HTTP 鉴权/重放/乱序、夹具指标）
python -m pytest -q

# 2) 重新生成确定性夹具
python -m scripts.make_fixtures

# 3) 四个场景的离线评价（ID 切换 / 位置误差 / 轨迹数 / 删除数）
python -m scripts.evaluate --all

# 4) 端到端：先启动服务，再跑签名客户端（正常、重放幂等、乱序409、伪造签名403）
uvicorn app.main:app --port 8000 &
MOT_BASE_URL=http://127.0.0.1:8000 python examples/client_demo.py

# 5) 推送示例输入（含一帧重复检测、一帧空帧）
MOT_BASE_URL=http://127.0.0.1:8000 python examples/load_sample.py
```

## 评价结果（本机实测，2026-09-23）

| 夹具 | 帧数 | ID 切换 | 确认轨迹数 | 平均位置误差 | 最大位置误差 | 删除 |
|---|---|---|---|---|---|---|
| crossing（交叉运动，最近约 0.8 m） | 25 | **0** | 2 | 4.9e-6 m | 8.3e-5 m | 0 |
| occlusion（A 遮挡 3 帧） | 18 | **0** | 2 | 3.5e-6 m | 4.6e-5 m | 0 |
| duplicates（每帧 A 重复一次） | 12 | **0** | 2 | 3.7e-3 m | 7.5e-3 m | 0 |
| empty_frames（连续 4 空帧） | 16 | **0** | 4（删除后重生） | 1.2e-5 m | 3.7e-5 m | 2 |

指标定义：每帧把存活的确认轨迹与真值做 1 m 门内贪心最近邻对应；某轨迹对应
的真值 ID 相对上一帧改变即计一次 ID 切换；位置误差为对应点对欧氏距离。

## 设计取舍

* 门控阈值夹具中取固定 7.0（约 χ²(2) 的 97% 分位），配合 1 cm 量测噪声，
  使交叉时刻错误配对落在门外、正确配对门内；API 默认仍为 99% χ² 分位
  （9.21），可在建会话时通过 `tracker` 参数覆盖。
* 帧内去重采用单链聚类（`duplicate_eps` 传递），对同一物体多份近似检测稳健。
* 会话与密钥保存在进程内存中（带锁），服务重启即失效；适合本地验收与
  单实例部署，生产需替换为持久化存储。
