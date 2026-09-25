# 运行记录（RUNLOG）

本文件**如实**记录开发/验收过程中实际执行的命令、真实输出摘要与
未能执行的项目，不做美化。机器：Linux 6.8.0-90-generic，Python 3.12.3。

## 环境准备

- 系统 Python 受 PEP 668 保护（`pip install numpy pytest` 报错
  `externally-managed-environment`），改用项目内虚拟环境：
  `python3 -m venv .venv`，然后 `.venv/bin/pip install numpy pytest pytest-cov`。
- 实际版本：numpy 2.5.3、pytest 9.1.1、pytest-cov 7.1.0 / coverage 7.16.1。
- **未通过/未执行项**：`ruff`、`bandit` 因环境网络/权限限制未能安装
  （`ERROR: No matching distribution found for ruff`；后续重试被权限分类器拒绝）。
  作为替代执行了 `python -m py_compile`（通过）与 pytest 全量测试。
  安全相关实现自查：无硬编码密钥、无外部 URL、服务仅监听 127.0.0.1、
  请求体有 1 MiB 上限、所有外部输入在边界处用 schema 校验并返回 400。

## 自动化测试

命令：

```bash
PYTHONPATH=src .venv/bin/python -m pytest --cov=pitjoin --cov-report=term-missing
```

结果：**69 passed**，总体语句覆盖率 **99%**（529 语句中仅 1 条未覆盖，
为 `cli.py` 的 `if __name__ == "__main__"` 守卫行）。各文件覆盖率：

| 文件 | 覆盖率 |
|---|---|
| engine.py | 100% |
| model.py | 100% |
| service.py | 100% |
| synthetic.py | 100% |
| times.py | 100% |
| serialize.py | 100% |
| __init__.py | 100% |
| cli.py | 99% |

开发过程中出现过 2 次真实失败，均已修复并复跑通过（非数据/逻辑缺陷）：

1. `test_revisions_ingest_later_than_past`（实际名
   `test_revisions_ingest_later_than_preliminary`）：测试自身字典查找
   漏写 `.replace("-final","-prelim")` → 修正测试后通过。
2. 初版泄漏实验中合成数据带 `0.05*day` 全局漂移，导致后期标签近乎全正类、
   时间外准确率反超训练集（指标失真）。去掉漂移、改为平稳序列后结果合理：
   训练 0.571 / 测试 0.615（防泄漏）vs 0.830 / 0.885（朴素），
   时间外虚高 **+0.271**，方向与幅度均符合"修订泄漏"预期。

## CLI 演示

### `demo`（规范场景手算对照）

```bash
PYTHONPATH=src .venv/bin/python -m pitjoin.cli demo
# exit=0
```

手算对照表 10 行全部 PASS：

```text
[PASS] E1 cust_A @T0+12h      f_balance reason 期望=SELECTED    实际=SELECTED    value 期望=100.0 实际=100.0
[PASS] E2 cust_A @T0+1d12h    f_balance reason 期望=SELECTED    实际=SELECTED    value 期望=110.0 实际=110.0
[PASS] E3 cust_A @T0+3d12h    f_balance reason 期望=SELECTED    实际=SELECTED    value 期望=111.0 实际=111.0
[PASS] E4 cust_B @T0+1d       f_score   reason 期望=SELECTED    实际=SELECTED    value 期望=3.0   实际=3.0
[PASS] E4 cust_B @T0+1d       f_tier    reason 期望=SELECTED    实际=SELECTED    value 期望=20.0  实际=20.0
[PASS] E5 cust_C @T0+1d       f_balance reason 期望=ALL_FUTURE  实际=ALL_FUTURE  value 期望=None 实际=None
[PASS] E6 cust_C @T0+11d      f_balance reason 期望=SELECTED    实际=SELECTED    value 期望=500.0 实际=500.0
[PASS] E7 cust_D @T0+1d       f_balance reason 期望=ALL_LATE    实际=ALL_LATE    value 期望=None 实际=None
[PASS] E8 cust_D @T0+6d       f_balance reason 期望=SELECTED    实际=SELECTED    value 期望=77.0 实际=77.0
[PASS] E9 cust_E @T0+1d       f_balance reason 期望=MISSING_KEY 实际=MISSING_KEY value 期望=None 实际=None

原因码统计： {'SELECTED': 7, 'MISSING_KEY': 18, 'ALL_FUTURE': 1, 'ALL_LATE': 1}
手算对照结果：10/10 通过
```

> 注：`MISSING_KEY=18` 是因为连接了全部 3 个特征：9 个事件中，
> 非所属实体的 (实体, 特征) 对都计为缺失；demo 输出只打印相关行。

关键裁决依据（真实输出）：

- **E2 挡住晚到修订**：`SELECTED -> record=rA2 v1 value=110.0
  (effective=2026-01-02T00:00Z, ingest=2026-01-02T01:00Z)
  | 1 条事后修订(ingest>event_time)被剔除，防止泄漏: rA3@ingest=2026-01-04T00:00Z`
- **E4 版本号裁决**：`record=rB3 v3 value=3.0 … 落选: rB1, rB2`
- **E4 入库时间裁决**：`record=rB5 v1 value=20.0 … 落选: rB4`
- **E7**：`ALL_LATE | 1 条事后修订…被剔除…: rD1@ingest=2026-01-06T00:00Z`
- **E5**：`ALL_FUTURE`；**E9**：`MISSING_KEY`

### `leakage`（模型对照）

```bash
PYTHONPATH=src .venv/bin/python -m pitjoin.cli leakage
# exit=0
```

```text
合成数据：1200 条准时初步值(v1, 带噪声) + 1200 条晚到定稿值(v2, 72h 后入库)，320 个事件
[防泄漏 PIT] 训练集 NaN 占比= 0.00%  训练准确率=0.571  时间外测试准确率=0.615
[朴素 as-of(会泄漏)] 训练集 NaN 占比= 0.00%  训练准确率=0.830  时间外测试准确率=0.885
离线时间外准确率虚高约 +0.271
```

## HTTP 服务与请求样例

```bash
PYTHONPATH=src .venv/bin/python -m pitjoin.cli serve --port 8000   # 仅监听 127.0.0.1
bash examples/requests.sh                                          # 6/6 个请求成功
```

实测响应已存档于 `examples/sample_responses.txt`。关键结果：

- `GET /health` → 200 `{"status":"ok"}`
- `/explain` 晚到修订 → 选 `rA2`，`rA3` 在 `excluded` 中且 `cause=LATE_INGEST`
- `/explain` 同时间多版本 → 选 `rB3` `version=3`，rB1/rB2 `TIE_LOST`
- 自带记录的 `/join`：修订入库前置空（取 v1=1000），入库后取 v2=1200；
  完全重复记录计 `DUPLICATE` 并稳定取首条；reason_counts=`{SELECTED:1, DUPLICATE:1}`
- `use_event_as_of=false` 对照请求在 E2 时刻错误返回 `f_balance=111.0`（泄漏）
- 非法输入（缺字段 / 坏 JSON / 坏时间戳 / 空事件数组 / 未知 dataset /
  非对象请求体 / 超 1 MiB / 未知路由）均返回 400/404，均有自动化测试覆盖。

## 遗留/未做

- 未做前端（按要求）。
- 未安装 ruff / bandit（环境限制，见上）；已用 py_compile + 69 个测试替代，
  但这不等于正式静态检查，若在有网络的环境应补跑。
- 服务为单进程演示用途，未做持久化、认证鉴权与并发压测；
  定位是本地机制验证，不是生产服务。
