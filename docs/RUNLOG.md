# 运行记录（RUNLOG）

本文件如实记录在开发机上的实际命令、结果与修复过程。所有命令均在项目根目录
`/home/admin/Downloads/biaozhul/opp159/a` 下执行。

## 1. 环境

```text
OS:      Linux 6.8.0-90-generic (Ubuntu)
Python:  3.12.3
numpy:   2.5.3
pytest:  9.1.1
coverage: 7.16.1
```

仅依赖 Python 标准库与 NumPy；未连接任何硬件，未使用 ROS，无前端/可视化。

## 2. 最终测试结果（干净复跑）

命令：

```bash
python3 -m pytest -q
```

结果：

```text
........................................................................ [ 83%]
..............                                                           [100%]
86 passed in 0.68s
```

按标记：

```bash
python3 -m pytest -m unit        # 77 passed, 9 deselected
python3 -m pytest -m integration #  9 passed, 77 deselected
```

覆盖率：

```bash
python3 -m coverage run -m pytest -q
python3 -m coverage report --include="sensor_matcher/*"
```

```text
Name                          Stmts   Miss  Cover
-------------------------------------------------
sensor_matcher/__init__.py        5      0   100%
sensor_matcher/__main__.py       26      2    92%
sensor_matcher/clock.py          34      1    97%
sensor_matcher/json_api.py      111      1    99%
sensor_matcher/matcher.py       165      2    99%
sensor_matcher/models.py         37      0   100%
sensor_matcher/offline.py        47      1    98%
sensor_matcher/pipeline.py       54      0   100%
sensor_matcher/synthetic.py      88      4    95%
-------------------------------------------------
TOTAL                           567     11    98%
```

总体行覆盖率 **98%**（高于 80% 要求）。未覆盖的 11 行均为防御性分支
（例如重复 `flush()` 的早退、个别不可能触发的入参校验分支）。

## 3. CLI / JSON 入口实际运行

### 3.1 基础消息样例

```bash
python3 -m sensor_matcher examples/messages_basic.json
```

结果（摘要）：

```text
online pairs : a0000-b0000, a0001-b0001, a0002-b0002, a0003-b0003
online rejects:
  a0004 / b0004  expired_no_candidate      # 2.0 与 2.06 相差 0.06 > 0.05
  a0005 / b0005  end_of_stream_unmatched    # 3.0 与 5.0，流结束仍未配
offline pairs: 与 online 相同（4 对）
consistent: True
```

其中 `a0002/a0003` 与 `b0002/b0003` 是**同一时间戳的重复消息**（t=1.0），
按 `(id_a, id_b)` 字典序确定性配对为 `a0002-b0002`、`a0003-b0003`。

### 3.2 时钟偏移校正

```bash
python3 -m sensor_matcher examples/messages_clock_offset.json
```

```text
a1 b1  raw_dt=0.205  corrected_dt=0.005
a2 b2  raw_dt=0.210  corrected_dt=0.010
a3 b3  raw_dt=0.190  corrected_dt=-0.010
consistent: True
```

请求给出 `clock_offset_b = -0.2`（B 钟慢 0.2 s），校正后时间差全部收敛到
容差 0.05 内；若不校正，|raw_dt|≈0.2 全部无法配对。

### 3.3 乱序到达

```bash
python3 -m sensor_matcher examples/messages_out_of_order.json
```

到达顺序为 `b3,a3,b2,a2,b1,a1`（严格逆序），`max_out_of_orderness=2.0`：

```text
pairs: a1-b1, a2-b2, a3-b3
rejects: []
consistent: True
```

### 3.4 完整合成实验

```bash
python3 -m sensor_matcher examples/synthetic_full.json
```

```text
estimated_offset = 0.23011        # 真值校正量 +0.23（B 钟 offset=-0.23）
summary = {
  "online_matches": 112,
  "online_rejects": 80,
  "reject_breakdown": {"expired_no_candidate": 77,
                       "end_of_stream_unmatched": 3},
  "offline_matches": 112,
  "offline_unmatched_a": 65,
  "offline_unmatched_b": 15
}
consistent = True
max|corrected dt| = 0.0729        # <= tolerance 0.08
```

该合成流包含：10 Hz 与 7 Hz 两种频率、传输延迟抖动导致的乱序到达、
B 钟 −0.23 s 恒定偏移、2% 随机丢包、A 在 7–9 s 与 B 在 13–14.5 s 的中断
间隙（产生过期）、以及同时间戳重复消息。在线与离线配对边集一致。

### 3.5 非法请求

```bash
python3 -m sensor_matcher examples/invalid_request.json
# stderr:
{"error": "tolerance 不能为负"}
# 退出码: 2
```

### 3.6 标准输入

```bash
echo '{"mode":"messages","tolerance":0.1,"streams":{"a":[{"id":"x","timestamp":0}],"b":[{"id":"y","timestamp":0.05}]}}' \
  | python3 -m sensor_matcher
# online pair: x-y，退出码 0
```

## 4. 过程中出现过的失败与修复（如实记录）

首次运行测试时有 **6 项失败 / 62 项通过**，逐项定位与修复如下：

1. `test_messages_mode_*`（3 项）`NameError: name 'Match' is not defined`
   —— `json_api.py` 使用了 `Match` 回填校正前时间戳却未导入。
   修复：补充 `from .models import Match`。

2. `test_out_of_order_arrival_still_matches`
   —— 测试把 δ 设为 0.05，但给定的严格逆序到达实际需要 δ≈1.02。这是
   **测试前提错误**，不是算法错误（δ 小于真实乱序量时水位线假设不成立）。
   修复：将该用例的 `max_out_of_orderness` 改为 1.02 并注明原因。

3. `test_duplicate_spec_shares_timestamp_and_seq`
   —— 重复消息在原实现里取到了自增后的序号（生成 `b0003-d1` 而非
   `b0002-d1`）。这是**生成器实现缺陷**。修复：重写
   `make_sensor_events` 的消息构造，重复消息复用当前序号与完全相同的
   时间戳/到达时间，id 为 `<seq>-d1`。

4. `test_recovers_constant_offset_without_noise`
   —— 测试期望 50 个点全部参与估计，但默认双侧裁剪 10% 实际用 40 个点。
   这是**测试期望写错**。修复：断言 `n_used == 40`。

修复后 68 项全部通过。

### 自查发现并修复的一个真实算法缺陷

在补测阶段自查水位线安全性时发现：仅凭“某个消息已成熟”就判过期/提交
**并不安全**。容差 10 的反例：已到 `a@0, b@10, a'@10.5` 时 `a@0` 已满足
成熟条件，旧逻辑会把它与 `b@10` 锁定；但随后到达的 `b'@10.6` 会让 `a'`
改配 `b'`，离线结局反而是 `a@0-b@10`、`a'@10.5-b'@10.6`——此时旧逻辑虽
碰巧配对正确，但在一般“成熟消息当前无候选”的情形下会**错误地提前过期**。

修复：把结算粒度从“单条成熟边/单个成熟顶点”改为**候选图连通分量封闭**
——仅当一个连通分量内所有顶点都成熟（未来消息不可能再连入）时，才在该
分量内整体提交配对与拒绝。新增回归测试
`test_ripple_augmenting_chain_does_not_expire_too_early` 锁定该行为。
修复后全部测试通过（随后扩充至 86 项）。

### 其他自查修正（非测试失败）

- 成熟判定由 `t + tol <= wm` 改为严格 `<`：水位线只保证不会再有时间戳
  小于 `wm` 的消息，相等时仍可能到达距离恰为容差的边界候选。
- 缓存溢出消息的时间戳仍计入观测最大值（水位线刻画“到达完整性”，与消息
  是否因缓存满被丢弃无关）。
- 乱序有界量 δ 改在**时钟校正后**的统一时基上计算，避免 B 钟有恒定偏移时
  跨流比较高估 δ。
- 一个依赖浮点微差的平局测试改为整数时间戳的显式平局用例，消除歧义。

## 5. 当前结论

- 全部 **86** 个自动化测试通过（77 单元 + 9 集成），覆盖率 **98%**。
- 验收点均有对应实现与测试：
  - 乱序、不同频率合成流对照离线定义：`tests/test_pipeline.py`、
    `tests/test_matcher.py::test_online_matches_offline_under_random_shuffles`；
  - 同时间重复消息：`test_duplicate_timestamps_*` 及基础样例；
  - 时钟偏移校正：`tests/test_clock.py`、3.2 / 3.4 实跑；
  - 过期：`test_expired_reason_for_unmatched_old_message`、
    端到端 `test_expired_rejects_appear_from_gaps` 及原因分类；
  - 有界缓存：`test_buffer_overflow_reason`、
    `test_buffer_overflow_is_flagged_and_consistency_marked_false`。
- 未通过项：开发过程中出现的失败均已修复并复测通过，**当前无未通过测试**。
