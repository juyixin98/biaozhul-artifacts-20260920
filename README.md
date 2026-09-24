# 异步 EKF 融合服务（纯后端）

二维位置 / 速度的扩展卡尔曼滤波融合服务。接收**不同频率**的里程计（odometry）与
GNSS 合成消息，**严格按测量时间**而非到达顺序融合：允许 2 秒内迟到消息，从检查点
缓存**重放**；超出窗口拒绝；协方差稳定更新（对称 + 半正定保证）；离群测量按
χ² 门限拒绝并留存证据。所有写接口使用真实 **HMAC-SHA256** 签名鉴权。

技术栈：Python 3.12 · NumPy · FastAPI · Uvicorn · Pydantic v2 · pytest。

---

## 1. 目录结构

```
app/
  config.py     # 环境变量配置（窗口、门限、过程噪声、HMAC 密钥等）
  crypto.py     # 真实 HMAC-SHA256 签名/验签（hmac.compare_digest + 新鲜度窗口）
  schemas.py    # 协议模型（测量、批量、健康、拒绝证据）
  ekf.py        # 恒速 EKF：Joseph 稳定更新、对称化、特征值 PSD 修复
  fusion.py     # 按测量时间排序、检查点重放、2s 窗口、门限拒绝、证据留存
  main.py       # FastAPI 路由
examples/
  make_examples.py  # 生成真实轨迹的有序/乱序/坏协方差示例输入
  send_signed.py    # 带 HMAC 签名的命令行客户端
  data/*.jsonl      # 已生成示例（143 条 12s 轨迹）
tests/          # 32 个自动化测试
requirements.txt / requirements.lock
```

## 2. 本地启动

```bash
# 1) 安装依赖（精确版本可复现）
python3 -m pip install -r requirements.lock
# 或宽松约束：python3 -m pip install -r requirements.txt

# 2) 生成示例输入（仓库已附带，可重新生成）
python3 -m examples.make_examples

# 3) 启动服务（生产请务必注入自己的密钥）
export FUSER_HMAC_SECRET="$(openssl rand -hex 32)"
python3 -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

可选环境变量：`FUSER_HORIZON_S`（默认 2.0）、`FUSER_Q`（过程噪声，默认 1.0）、
`FUSER_GATE_NIS`（默认 9.2103 = χ²₂ 99% 分位）、`FUSER_INIT_POS_VAR`、
`FUSER_INIT_VEL_VAR`、`FUSER_SIGN_FRESHNESS_S`（默认 300）。

## 3. 验收命令

```bash
# 自动化测试（32 项，无需起服务）
python3 -m pytest -v

# 起服务后端到端冒烟（另开一个终端）
python3 -m uvicorn app.main:app --host 127.0.0.1 --port 8000
export FUSER_HMAC_SECRET=<与服务端相同>

# 故意乱序发送（迟到 0.4~1.8s，全部落在 2s 窗口内）
python3 -m examples.send_signed reset
python3 -m examples.send_signed fuse examples/data/shuffled.jsonl
python3 -m examples.send_signed state     # 每步后的最终状态
python3 -m examples.send_signed trace     # 每步状态/创新量/NIS/卡尔曼增益
python3 -m examples.send_signed rejections# 离群与坏协方差的拒绝证据

# 对照：有序发送；两者最终状态与全部检查点轨迹逐位一致
python3 -m examples.send_signed reset
python3 -m examples.send_signed fuse examples/data/ordered.jsonl

# 批量接口（内部仍按测量时间重放）
python3 -m examples.send_signed batch examples/data/shuffled.jsonl

# 坏协方差输入（非对称 / 非半正定）→ HTTP 422 并留证据
python3 -m examples.send_signed fuse examples/data/bad_covariance.jsonl
```

接口文档：服务启动后访问 `http://127.0.0.1:8000/docs`（OpenAPI/Swagger UI）。

## 4. 协议

### 4.1 测量消息（`POST /fuse`，需签名）

```json
{
  "id": "gnss-0007",
  "type": "gnss",
  "time": 3.25,
  "measurement": [3.21, 1.68],
  "R": [[0.6, 0.03], [0.03, 0.5]],
  "seq": 0,
  "gate_nis": 9.21
}
```

- `type = "odometry"`：`measurement = [vx, vy]`
- `type = "gnss"`：`measurement = [px, py]`
- `time`：测量时间（秒，任意单调时钟基准；与签名墙钟时间相互独立）
- `R`：**2×2 测量协方差，必须有限、对称、半正定**，否则 422 拒绝
- `seq`：同一时刻多消息的确定性次序（可选）
- `gate_nis`：覆盖本条消息的马氏距离门限（可选）

`POST /fuse/batch`：`{"measurements": [ ... ]}`，结果与输入一一对应。

### 4.2 响应（每步状态 + 创新量）

```json
{
  "id": "gnss-0007", "accepted": true, "status": "processed",
  "replayed": 3,
  "step": {
    "dt": 0.15,
    "predicted": {"time": 3.25, "x": [3.10, 1.55, 1.0, 0.5]},
    "P_predicted": [[...4x4...]],
    "innovation": [0.11, 0.13],
    "S": [[...2x2...]], "K": [[...2x4...]],
    "nis": 0.42, "gate_nis": 9.2103,
    "accepted": true, "gated": false,
    "posterior": {"time": 3.25, "x": [...]},
    "P": [[...4x4...]],
    "post_min_eigenvalue": 0.0031
  },
  "state": {"time": 3.25, "position": [...], "velocity": [...], "x": [...], "P": [...]}
}
```

拒绝：`accepted=false`，`reason ∈ {OUTLIER_GATE, LATE_OUT_OF_HORIZON, BAD_COVARIANCE}`，
`evidence.detail` 给出 NIS、创新量、S、迟到秒数或最小特征值等可核查证据。

### 4.3 查询与控制

| 方法/路径 | 签名 | 说明 |
|---|---|---|
| `GET /health` | 否 | 存活与缓存规模 |
| `GET /state` | 否 | 当前状态与协方差 |
| `GET /trace?limit=` | 否 | 窗口内每步：预测/创新量/S/K/NIS/后验 |
| `GET /rejections?limit=` | 否 | 离群、超窗、坏协方差拒绝证据（最新在前） |
| `POST /fuse` | 是 | 融合单条 |
| `POST /fuse/batch` | 是 | 融合一批 |
| `POST /reset` | 是 | 清空引擎 |

## 5. 密码学（真实执行）

写接口要求两个头：

```
X-Timestamp: <客户端 UNIX 秒>
X-Signature: hex( HMAC_SHA256(secret, "METHOD\nPATH\nX-Timestamp\nsha256(body)") )
```

- 标准库 `hmac` / `hashlib` 实现（无占位、无 mock）。
- `hmac.compare_digest` 常量时间比较；签名时间戳有 ±`FUSER_SIGN_FRESHNESS_S`
  新鲜度窗口，防重放；摘要覆盖方法、路径、时间戳与请求体，防篡改。
- 无签名 / 错签名 / 篡改正文 / 过期时间戳一律 **401**（测试覆盖）。

## 6. 核心算法说明

**状态** `x = [px, py, vx, vy]ᵀ`，恒速转移
`F = I + dt·(位置←速度)`，过程噪声采用连续白噪声加速度离散化
（`Q` 含 `q·dt²/2`、`q·dt⁴/4` 项）。

**稳定协方差更新**：

1. 预测后、更新后都做对称化 `P ← (P+Pᵀ)/2`；
2. 更新采用 **Joseph 形式** `P = (I−KH)P(I−KH)ᵀ + KRKᵀ`；
3. 特征值分解，将负特征值裁剪到正下限，保证 **PSD**（轨迹里同时给出修复前/后
   最小特征值以便审计）；创新协方差 `S` 经特征分解稳定求逆。

**按测量时间的异步语义**（不按到达顺序直接融合）：

- 测量全序 = `(time, seq, sensor_rank, id)`；
- 新测量先入有序缓存，再找到其前一个**检查点**，恢复状态后把之后的测量全部
  **重放**（包括它自己），因此迟到消息会修正所有后续状态；
- 每步保存状态检查点；缓存随 2s 窗口滚动裁剪，但始终保留一个可回放锚点；
- 早于 `latest_time − 2s` 的消息 **拒绝**（`LATE_OUT_OF_HORIZON`），不入缓存；
- 同 `id` 幂等去重。

**门限**：创新马氏平方距离 `NIS = νᵀS⁻¹ν`，超过门限（默认 χ²₂ 的 99% 分位
9.2103）拒绝测量修正，但时间照常前推，证据写入 `/rejections`。

## 7. 测试覆盖（32 项）

- `test_ekf.py`：预测单调性、Joseph 更新、PSD 修复、里程计/GNSS 观测模型；
- `test_replay.py`：**乱序与有序逐位一致**（示例文件 + 6 组随机到达延迟）、
  迟到消息从检查点重放并改变后续状态、GNSS 先到/里程计迟到的初始化重建；
- `test_time.py`：超 2s 拒绝、边界接受、时间回跳、31s 长缺测协方差膨胀与
  GNSS 恢复、幂等去重、同刻确定性排序；
- `test_gating.py`：离群拒绝与证据、逐条门限覆盖、协方差五类非法形态；
- `test_api.py`：真实 HMAC-SHA256（含手工 hmac 计算）、无签名/篡改/错密钥/
  过期 → 401、422 坏协方差、200 离群拒绝、批量重放。

## 8. 失败如实报告的设计取舍

- 坏协方差**不做“修复后使用”**：直接拒绝并返回最小特征值等证据，避免静默写错状态；
- 离群拒绝只拒绝测量修正，预测仍推进，状态时间不回退、不卡断；
- 重放是确定性的（固定全序），因此“乱序到达”与“有序到达”的所有检查点结果
  必须逐位相等——这是最硬的验收标准，由自动化测试强制。
