# 多传感器时间配对（Multi-Sensor Temporal Pairing）

纯后端离线计算库：把两类传感器消息按**时间容差**进行 1:1 配对。仅使用
Python 标准库与 NumPy，**只处理合成轨迹与合成传感器数据**，不连接任何硬件、
不依赖 ROS、不含任何可视化或前端代码。

核心能力：

- 明确的**离线权威定义**：最近匹配、单次消费（每条消息最多用一次）、
  确定性的平局规则。
- **有界缓存的在线算法**：消息可乱序到达；基于事件时间水位线，只在结果
  不可能再被未来消息改变时提交；对无法匹配的消息给出具体原因。
- **时钟偏移校正**：用同时刻校准点对估计两钟恒定偏移，在统一时基上配对。
- **合成数据验证**：乱序、不同频率、重复时间戳、时钟偏移、丢包/中断间隙
  全部由合成器产生，在线结果逐条对照离线定义。
- JSON 入口（文件或标准输入）+ 命令行 + 自动化测试。

---

## 1. 配对语义

### 1.1 离线权威定义（oracle）

给定两流消息（时间戳已校正到统一时基）与非负容差 `tolerance`：

1. **候选边**：所有满足 `|t_b − t_a| <= tolerance` 的 `(a, b)` 对，边权为
   时间距离 `|t_b − t_a|`。边界距离（恰好等于容差）计入候选。
2. **最近匹配（贪心最近邻）**：候选边按下面的**全局确定顺序**逐条考虑：
   1. 时间距离 `|dt|` 升序（最近优先）；
   2. 距离相同 → 边两端中**较早时间戳** `min(t_a, t_b)` 升序；
   3. 仍相同 → 较晚时间戳 `max(t_a, t_b)` 升序；
   4. 仍相同（例如完全同时间戳的重复消息）→ `(id_a, id_b)` 字典序。
3. **单次消费（1:1）**：一条边被接受后，其两端消息立即标记为已消费，
   之后任何涉及它们的边一律跳过。每条消息最多出现在一个配对中。

结果只依赖消息内容（时间戳 + id），**与输入数组顺序无关**。

> 说明：这是“按全局边顺序的最近邻贪心”，刻意给出一个简单、确定、可流式
> 增量维护的定义，并不宣称是加权二分图的全局最优（最大权匹配）。在容差
> 远小于消息间隔的常见情形下它与“每点取最近邻”一致。

### 1.2 在线（有界缓存、乱序）算法

实现在 `sensor_matcher/matcher.py`。

- 每流维护观测到的最大时间戳 `M` 与乱序有界量 `δ`
  （`max_out_of_orderness`）：假设任何迟到消息的时间戳不小于 `M − δ`。
  全局**水位线** `wm = min(M_a − δ, M_b − δ)`。
- 消息 `m` 在 `t_m + tolerance < wm` 时**成熟**（严格不等号：等于时仍可能
  到达一条距离恰为容差的边界候选）。
- 在当前缓存上构造候选二分图，按其**连通分量**结算：
  - 一个分量**封闭** ⟺ 其中所有顶点都成熟。此时未来消息（时间戳
    `>= wm`）不可能再与该分量的任何顶点相连，分量内的结局就此确定。
  - 封闭分量内按 §1.1 的同一套边顺序做贪心，提交配对；分量内未配对的
    消息判为无法匹配；孤立成熟顶点作为单顶点分量处理。
  - 尚未封闭的分量**一律不动**，等后续水位线推进。
- **为什么必须按连通分量、而不能“某个消息一成熟就结算”**：未来消息可能
  先抢走候选链上的对端，经“增广链”连锁改变成熟消息的归属。例如容差 10：
  `a@0—b@10—a′@10.5` 中 `a@0` 虽已成熟，但 `b′@10.6` 一旦到达，`a′` 会改配
  `b′`，于是 `a@0` 反而应与 `b@10` 配对。只有整条链所在的分量封闭才安全。
  该场景有回归测试 `test_ripple_augmenting_chain_does_not_expire_too_early`。
- 全部消息送完后调用 `flush()`，水位线视为 +∞，提交所有剩余结果。

**正确性**：在不发生缓存溢出丢弃的前提下，在线算法最终的配对边集与离线
权威定义**完全一致**（由随机性质测试与端到端测试验证）。封闭分量之外的
消息不动，因此后到的消息无法回改已提交结果。

### 1.3 无法匹配的原因

| 原因常量 | JSON 中的值 | 含义 |
|---|---|---|
| `REASON_EXPIRED` | `expired_no_candidate` | 其连通分量已封闭，分量内争不到任何候选；未来也不可能再有候选。 |
| `REASON_BUFFER_OVERFLOW` | `buffer_overflow` | 该流有界缓存已满，此消息被直接拒绝、不进入缓存（不淘汰旧数据）。 |
| `REASON_END_UNMATCHED` | `end_of_stream_unmatched` | `flush()` 后仍未形成配对。 |

发生 `buffer_overflow` 意味着有消息在参与配对前被丢弃，此时在线结果可能
少于离线结果，响应中的 `consistent` 会如实置为 `false`。

### 1.4 时钟偏移校正

`sensor_matcher/clock.py`：假设 B 钟相对 A 钟有近似恒定偏移
`t_true = t_b_raw + offset`。用一小段**同时刻校准点对**（合成器生成）的
残差 `raw_a − raw_b`，经双侧裁剪 + 中位数稳健估计 `offset`，随后
`t_b_corrected = raw_b + offset`，再在统一时基上做容差配对。结果同时记录
校正前 `dt_raw` 与校正后 `dt_corrected` 以便审计。

---

## 2. 安装与运行

需要 Python 3.10+（开发环境为 3.12）与 NumPy。

```bash
pip install -r requirements.txt

# 用请求文件
python -m sensor_matcher examples/synthetic_full.json

# 或从标准输入
echo '{"mode":"messages","tolerance":0.1,"streams":{...}}' | python -m sensor_matcher
```

成功时结果 JSON 写到 stdout（退出码 0）；请求非法时错误 JSON 写到 stderr
（退出码 2）。

---

## 3. JSON 请求格式

### 3.1 `messages` 模式：直接提供两条消息流

```json
{
  "mode": "messages",
  "tolerance": 0.05,
  "max_buffer_size": 1000,
  "max_out_of_orderness": 0.0,
  "clock_offset_b": -0.2,
  "streams": {
    "a": [{"id": "a1", "timestamp": 0.0, "payload": null}],
    "b": [{"id": "b1", "timestamp": 0.21, "payload": null}]
  },
  "arrival": [["a", "a1"], ["b", "b1"]]
}
```

| 字段 | 说明 |
|---|---|
| `tolerance` | 必填，非负时间容差（秒）。 |
| `max_buffer_size` | 每流缓存容量，默认 1000。 |
| `max_out_of_orderness` | 乱序有界量 δ（秒），默认 0（顺序到达）。 |
| `clock_offset_b` | B 钟校正量，`t_b_corrected = raw_b + clock_offset_b`，默认 0。 |
| `streams.a` / `streams.b` | 消息数组，每条含非空字符串 `id`、数字 `timestamp`、可选 `payload`。同流 id 不得重复。 |
| `arrival` | 可选，`["a"|"b", id]` 数组，显式给出（可乱序的）到达顺序；须恰好包含每条消息一次。缺省为先 A 后 B、按数组顺序。 |

### 3.2 `synthetic` 模式：生成合成轨迹与双频传感器流

```json
{ "mode": "synthetic", "config": { "...": "见 examples/synthetic_full.json" } }
```

`config` 常用键：

- 顶层：`tolerance`、`duration`（轨迹时长，秒）、`traj_dt`、
  `max_buffer_size`、`auto_out_of_orderness`（默认 true，按实际到达顺序自动
  确定 δ）、`max_out_of_orderness`（关闭自动时使用）。
- `sensor_a` / `sensor_b`：`rate_hz`、`clock_offset`、`timestamp_jitter`、
  `delay_mean`、`delay_jitter`（产生乱序到达）、`drop_prob`、
  `gap_intervals`（`[起点, 终点]` 的中断区间，制造过期）、
  `duplicate_spec`（产生同时间戳重复消息的采样序号数组）、`seed`。
- `calibration`：`n_points`、`jitter`、`trim_ratio`、`seed`。

### 3.3 响应

两种模式都返回：

- `online.matches` / `online.rejects`：有界缓存在线结果（含每条拒绝的原因）。
- `offline.matches` / `offline.unmatched`：离线权威定义结果。
- `consistent`：未发生溢出且在线边集与离线边集相等时为 `true`。
- `summary`：计数与按原因的拒绝统计。
- 合成模式另有 `clock.estimated_offset`。

---

## 4. 请求样例

| 文件 | 演示内容 |
|---|---|
| `examples/messages_basic.json` | 基础配对、同时间戳重复消息、过期与流结束未配 |
| `examples/messages_clock_offset.json` | B 钟偏移 −0.2 的校正 |
| `examples/messages_out_of_order.json` | 显式逆序 `arrival` + δ |
| `examples/synthetic_full.json` | 10 Hz / 7 Hz 双频、乱序、偏移、丢包、中断、重复的完整合成实验 |
| `examples/invalid_request.json` | 非法请求（负容差），退出码 2 |

---

## 5. 自动化测试

```bash
python -m pytest                      # 全部测试（含 unit / integration 标记）
python -m pytest -m integration
python -m coverage run -m pytest && python -m coverage report --include="sensor_matcher/*"
```

测试组织：

- `tests/test_offline.py`：最近匹配、单次消费、各层平局规则、输入校验。
- `tests/test_matcher.py`：乱序到达、水位线成熟提交、过期/溢出/流结束原因、
  增广链回归、随机时间戳 + 随机乱序下“在线边集 == 离线边集”的性质测试。
- `tests/test_clock.py`：偏移估计、噪声、裁剪抗离群、入参校验。
- `tests/test_synthetic.py`：轨迹插值、频率/偏移、重复时间戳、中断、丢包、
  乱序界 δ、校准点对。
- `tests/test_pipeline.py`：端到端合成流在线/离线一致性、时钟校正效果、
  过期来源、单次消费、溢出标记、多种子稳定性。
- `tests/test_json_api.py` / `tests/test_cli.py`：JSON 两种模式、校验失败、
  命令行与 `python -m` 子进程。

实际运行的命令与输出（含覆盖率数字、未通过项的修复过程）见
[`docs/RUNLOG.md`](docs/RUNLOG.md)。

---

## 6. 代码结构

```
sensor_matcher/
  models.py      不可变数据模型 Message / Match / Reject 与原因常量
  clock.py       时钟偏移估计（裁剪中位数）与校正
  offline.py     离线权威定义（候选边 + 全局边序贪心 + 单次消费）
  matcher.py     有界缓存、水位线、连通分量封闭结算的在线匹配器
  synthetic.py   合成轨迹、双频传感器流、乱序/重复/偏移/丢包/中断生成
  pipeline.py    端到端编排：生成→校正→在线→离线→一致性对照
  json_api.py    JSON 请求解析、边界校验与处理
  __main__.py    命令行入口
examples/        请求样例
tests/           pytest 测试
docs/RUNLOG.md   实际运行记录
```

## 7. 已知边界

- 贪心最近邻不是全局最优加权匹配；这是有意选择的确定性语义。
- 在线结算每次重算缓存内候选图与连通分量，单步为缓存规模的平方量级；
  缓存有界且用于离线合成验证，未做面向高吞吐实时流的索引优化。
- 时钟模型只覆盖**恒定偏移**，不含漂移（slope）与温度相关变化。
- 时间均为双精度浮点秒；平局规则依赖严格的数值比较。
