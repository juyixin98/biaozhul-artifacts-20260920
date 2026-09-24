# 设备时钟漂移拟合服务（Device Clock Drift Calibration）

纯后端离线校准服务：输入多轮「请求发出 / 响应到达」时间戳与设备计数器读数，
估计**设备计数器 → 主机时间**的偏移与线性漂移，识别计数器回绕与设备重启、
主机/设备时间跳变、漂移突变，过滤异常往返时延，并在证据不足时如实返回
**不确定**而不是强行给一个答案。所有估计都给出**误差区间**，不假设网络
上下行延迟对称。

- Python 3.12 · NumPy（真实数值计算）· FastAPI（纯 API，无前端页面）
- SQLite 持久化校准版本与每次转换所用版本（历史可追溯）
- **Ed25519（RFC 8032）真实签名**：每个发布版本带可验证签名与公钥，
  签名在首次启动时用系统 CSPRNG 真实生成（`cryptography` 库，非占位）

---

## 1. 快速开始

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt          # 或精确复现：pip install -r requirements.lock.txt

# 生成示例输入（examples/*.json）
python scripts/generate_examples.py

# 启动服务
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

数据默认存放在 `./data/`（可用环境变量 `CLOCKCAL_DATA_DIR` 覆盖），
首次启动会在其中生成权限为 `0600` 的 Ed25519 私钥 `ed25519_private.pem`。

## 2. 验收命令

```bash
source .venv/bin/activate

# (a) 自动化测试（37 个：数值、分段、密码、API 全链路）
python -m pytest -q

# (b) 端到端验收：先启动服务（见上），另开一个终端执行
bash scripts/acceptance.sh
```

`acceptance.sh` 会：跑测试 → 离线分析全部 8 个场景 → 发布签名版本 →
转换并校验区间 → 查询历史 → 验证完整签名通过、篡改模型被拒绝。

手动试一次：

```bash
curl -s localhost:8000/health
curl -s -X POST localhost:8000/api/v1/calibrations/analyze \
  -H 'Content-Type: application/json' --data-binary @examples/asymmetric.json | jq .
```

---

## 3. 输入模型

每个样本是一轮请求/响应：

| 字段 | 含义 |
|---|---|
| `t_send`  | 主机时钟下，请求离开主机的时刻（秒，如 unix 时间） |
| `t_recv`  | 主机时钟下，响应回到主机的时刻（`t_recv >= t_send`） |
| `counter` | 响应中设备报告的计数器读数（任意线性单位，如 tick） |

```json
{
  "device_id": "demo-asymmetric",
  "modulus": 10000.0,
  "samples": [
    {"t_send": 1700000000.004, "t_recv": 1700000000.020, "counter": 12.3},
    {"t_send": 1700000000.504, "t_recv": 1700000000.521, "counter": 505.8}
  ]
}
```

`modulus` 是计数器模数；**不知道就传 `null`**——此时回绕与重启无法区分，
服务会把该事件标为 `wrap_or_restart` 并把整体状态降为 `uncertain`。

---

## 4. API

| 方法 & 路径 | 说明 |
|---|---|
| `GET  /health` | 存活检查 + 签名算法与公钥 |
| `POST /api/v1/calibrations/analyze` | 仅分析，返回分段/事件/区间，不落库 |
| `POST /api/v1/calibrations/publish` | 分析并发布**带签名**的校准版本 |
| `GET  /api/v1/devices/{id}/versions` | 该设备全部历史版本（含签名封套） |
| `GET  /api/v1/versions/{version_id}` | 取指定版本 |
| `POST /api/v1/convert` | 用「在指定主机时刻生效」的版本把计数器转主机时间，记录历史 |
| `GET  /api/v1/devices/{id}/history` | 转换历史，每行含所用 `version_id` |
| `POST /api/v1/verify` | 校验签名封套（真实 Ed25519 验签） |

发布参数：`valid_from` / `valid_to`（生效区间，秒；省略 `valid_to` 表示开放）、
`force`（状态非 `ok` 时必须显式置 `true` 才允许发布；证据不足到无法拟合时
即使 `force` 也会拒绝）。新发布的开放版本会**自动截止（supersede）**旧开放
版本；与已有**闭合**区间重叠返回 `409`。

转换返回的是区间，不是一个假精确的点：

```json
{"host_time": {"point": 1700000029.516, "lower": 1700000029.508,
               "upper": 1700000029.524}}
```

---

## 5. 计算方法（关键：不假设对称延迟）

每个样本只给出真实打戳时刻所在的**区间约束**：

```
t_send_i ≤ α + β·c_i ≤ t_recv_i          h = α + β·c
```

不做「往返时延对半分」假设。上行比下行慢 100 ms 也只会进入 **α 的不确定
区间**，不会偷偷污染漂移率 β。

- **漂移率 β 的硬区间**：枚举样本对
  `(lo_i−hi_j)/(c_i−c_j) ≤ β ≤ (hi_i−lo_j)/(c_i−c_j)`，取交集；
  并校验相同计数器样本的主机区间必须重叠。
- **点估计**：区间中点上的 Theil–Sen（配对斜率中位数），高崩溃点，
  对少数异常样本稳健；α 取可行区间中值。
- **任意计数器处的主机时间区间**：
  `α_lo(β)=max_i(lo_i−β·c_i)` 与 `α_hi(β)=min_i(hi_i−β·c_i)` 是分段线性
  包络；在 `[β_lo, β_hi]` 上枚举区间端点与所有两线交点（包络折点）精确求
  全局上下确界。测试 `tests/test_fitting.py` 在 1 ms/100 ms 极端非对称延迟
  下验证真值始终落在区间内。
- **异常 RTT 过滤**：单侧（只剔除**偏大**的往返，偏短的紧时序是有用信息），
  尺度用上半四分位距 `Q3−Q2`（对少数尖刺稳健，不被尖刺自身抬高）。
- **区间不一致修复**：未建模跳变会破坏配对约束，按最严重违例对、丢弃 RTT
  较大（或残差较大）的端点，最多裁剪 35%；超过则报 infeasible 而非硬算。
  可行但中点残差 ≥25 倍尺度的粗大离群点（如一次 5 s 卡顿）也会被剔除。

## 6. 事件识别（回绕 vs 重启，用证据区分）

沿时间扫描，先去 RTT 离群，再用相邻正向计数器移动的中位速率估粗漂移：

- **回绕 `wrap`**：计数器倒退必须能用 `k·模数`（k 为正整数）在粗漂移率与
  容差（max(5 MAD, 2 中位RTT)）内解释；回绕后计数器本来就接近 0，因此
  「接近 0」**本身不是重启证据**。识别后把计数器展开，同一段连续拟合。
- **重启 `restart`**：计数器大幅倒退到接近 0，且**没有任何整数圈数**能解释
  该时间间隔 → 开启新 epoch，分段拟合。
- **无法区分 `wrap_or_restart`**：两种故事都成立（例如不知道模数，或刚
  回绕又几乎立刻重启），整体状态置 `uncertain`，不替用户猜。
- **时间跳变 `time_jump`**：分段稳健（Theil–Sen）拟合，断点处比较两直线
  在**同一计数器**上的水平落差——纯跳变是「同一计数器对应两个主机时间」，
  阈值取 `max(6 倍一阶差分稳健尺度, 10 中位RTT)`；归因一律标
  `attribution: indeterminate`（主机跳还是设备跳，仅凭这些数据无法区分）。
- **漂移突变 `drift_change`**：两段速率 Theil–Sen 比值 ≥1.25、两侧样本充足、
  断点垂直落差≈0（连续拐点）；报告前后 β、比值与区间分离度。纯斜率变化
  与水平跳变通过「断点连续性」区分，斜坡不会被误判成一串跳变。
- **计数器停走 `counter_stall` / 计数缺口 `counter_gap`**：记录为证据事件。

## 7. 状态语义

- `ok`：推荐段样本充足、偏移半宽 ≤50 ms、漂移相对半宽 ≤0.2%、无模棱两可事件；
- `uncertain`：能拟合但不确定度超阈，或存在 `wrap_or_restart` 等证据不足事件；
- `insufficient_evidence`：RTT 过滤后可用样本不足（<6）或无计数器跨度。

阈值集中在 `app/config.py`，可在请求体 `config_overrides` 中逐项覆盖。

## 8. 示例场景（`examples/`，由脚本真实模拟）

| 文件 | 场景 |
|---|---|
| `asymmetric.json` | 下行 4 ms / 上行 12 ms 非对称，含 3 次回绕 |
| `rtt_spikes.json` | 上述非对称 + 三个 600 ms 拥堵尖刺（应被过滤） |
| `time_jump.json` | 中途主机时钟 +3 s 跳变（检测、归因不确定） |
| `drift_change.json` | 计数器速率连续地变为 1.5×（检测漂移突变） |
| `wrap.json` | 小模数，6 次干净回绕（展开成一段） |
| `restart.json` | 设备重启，计数器回零附近（新 epoch） |
| `unknown_modulus.json` | 模数未知却发生倒退 → 回绕/重启不可区分 → uncertain |
| `few.json` | 仅 8 样本：能拟合但不确定 → uncertain（禁止无 force 发布） |

## 9. 项目结构

```
app/
  fitting.py    区间回归、RTT 过滤、包络误差区间（NumPy）
  analyzer.py   分段、回绕/重启/跳变/漂移突变识别、状态判定
  config.py     全部阈值（显式、可覆盖）
  crypto.py     Ed25519 真实密钥生成/签名/验签、确定性 JSON
  storage.py    SQLite：设备、签名版本与生效区间、转换历史
  predict.py    用已发布版本把原始计数器转主机时间区间
  main.py       FastAPI 应用与全部端点
tests/          37 个 pytest 自动化测试
scripts/        示例生成器与端到端验收脚本
examples/       8 个示例输入
requirements.txt / requirements.lock.txt
```

## 10. 局限与诚实说明

- 恒定非对称延迟会让**中点估计的 α**带偏；该偏差无法在单次往返中消除，
  服务通过区间宽度如实暴露（`offset_halfwidth_s`），β 不受恒定非对称影响。
- 主机时钟跳变与设备时钟跳变仅凭请求/响应数据不可区分，事件归因恒为
  `indeterminate`。
- 计数器模数未知时，倒退既可能是回绕也可能是重启，服务只报不确定。
- 区间是在「每段内计数器对主机时间线性」这一模型下的硬界；外推过远会在
  返回中带 `warning`，且区间随之展宽。
