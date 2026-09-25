# 特征时间点连接服务（Point-in-Time Join, pitjoin）

纯后端、纯本地的机器学习基础设施原型：实现**离线特征时间点连接**，
在构造训练/回放数据集时，按"实体 + 事件时刻"选择**当时真正可用**的
特征版本，并同时考虑数据入库时刻（`ingest_time`），从机制上杜绝
**事后修订泄漏（look-ahead / revision leakage）**。

- 语言：Python 3.10+，仅依赖 **NumPy**；HTTP 服务使用标准库 `http.server`
- 数据：全部为内置、**可复现的合成数据**（固定随机种子），不下载任何外部模型或数据
- 无前端、无外部网络调用；服务默认只监听 `127.0.0.1`

## 1. 要解决的问题

特征库里同一条特征往往有多个版本（初步值、修订值、补录值）。
朴素的 as-of join 只比较 `effective_time <= event_time`，于是事件发生后
才入库的"修订值/定稿值"会被错误地当作当时可见，造成离线指标虚高、
上线不可复现。

本服务选择的记录必须**同时**满足：

| 条件 | 含义 |
|---|---|
| `effective_time <= event_time` | 业务生效时间不晚于事件时刻（as-of） |
| `ingest_time <= event_time` | 事件发生的那一刻，记录已经入库、对下游可见 |

> 离线回放时以**事件时刻**作为可见性截止（`as_of = event_time`）：
> 我们假装站在事件发生的那一刻提问。

**同一时刻多版本**的确定性裁决优先级：
`max(effective_time)` → `max(ingest_time)` → `max(version)`；
三者完全相同则视为重复记录，稳定取首条并标记 `DUPLICATE`。

**时间边界语义**（整数毫秒比较，无浮点歧义）：

- `effective_time == event_time` → 生效，包含（`searchsorted(side="right")`）
- `ingest_time == event_time` → 恰在提问时刻可见，包含
- `ingest_time == event_time + 1ms` → 晚到，剔除（`LATE_INGEST`）

## 2. 快速开始

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt   # numpy；pytest 见下

# 1) 规范场景：晚到修订 / 同时间多版本 / 缺特征，逐事件手算对照
PYTHONPATH=src .venv/bin/python -m pitjoin.cli demo

# 2) 泄漏对照实验：防泄漏 PIT join vs 朴素 as-of join，各训一个逻辑回归
PYTHONPATH=src .venv/bin/python -m pitjoin.cli leakage

# 3) 启动本地 HTTP 服务
PYTHONPATH=src .venv/bin/python -m pitjoin.cli serve --port 8000
```

也可以开发模式安装：`.venv/bin/pip install -e ".[dev]"` 后无需 `PYTHONPATH`。

## 3. 验收场景（可手算对照）

`pitjoin.synthetic.build_canonical_dataset()` 内置如下确定性数据
（`T0 = 2026-01-01T00:00Z`），`demo` 命令会打印每条选择的完整依据，
并与手算期望逐条比对（实测 10/10 通过）：

| 情形 | 数据 | 事件 | 选中 / 原因 |
|---|---|---|---|
| 晚到修订 | `rA2` value=110（T0+1d 生效、+1h 入库）；`rA3` value=111（**同生效时间**、T0+3d 才入库） | T0+1d12h | **rA2=110**，rA3 以 `LATE_INGEST` 剔除 |
| 修订入库后 | 同上 | T0+3d12h | rA3=111 |
| 同时间多版本（版本号裁决） | `rB1/rB2/rB3` 生效与入库时刻完全相同，v1=1/v2=2/v3=3 | T0+1d | **rB3=3**，rB1/rB2 为 `TIE_LOST` |
| 同时间同版本（入库先后裁决） | `rB4` ingest=T0+2h（10）；`rB5` ingest=T0+5h（20） | T0+1d | **rB5=20**，rB4 为 `TIE_LOST` |
| 缺特征：未来生效 | `rC1` T0+10d 生效（虽早已入库） | T0+1d | `ALL_FUTURE` |
| 缺特征：全部晚入库 | `rD1` T0 生效但 T0+5d 才入库 | T0+1d | `ALL_LATE` |
| 缺特征：实体不存在 | cust_E 无任何记录 | T0+1d | `MISSING_KEY` |

每次选择都返回 `SelectionEvidence`：候选总数、as-of 候选数、
每条被剔除记录及原因（`FUTURE_EFFECTIVE / LATE_INGEST / TIE_LOST / DUPLICATE`）、
最终裁决理由。示例（E2 时刻，事后修订被挡住）：

```text
[entity=cust_A feature=f_balance] event_time=2026-01-02T12:00Z: SELECTED
 -> record=rA2 v1 value=110.0 (effective=2026-01-02T00:00Z, ingest=2026-01-02T01:00Z)
 | 1 条事后修订(ingest>event_time)被剔除，防止泄漏: rA3@ingest=2026-01-04T00:00Z
```

## 4. HTTP 接口

仅标准库实现，请求/响应均为 JSON，时间统一为 ISO-8601（UTC）。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 健康检查 |
| POST | `/join` | 批量时间点连接，返回特征宽表 + 每条选择依据 |
| POST | `/explain` | 单点选择，返回完整选择依据 |

请求体字段：

- `dataset`（可选）：`canonical`（默认）或 `generated`
- `records`（可选）：自带特征记录，给出后忽略 `dataset`
  - `entity_id, feature_name, value, effective_time, ingest_time, version?, record_id?`
- `events`（`/join`）：`[{entity_id, event_time, label?}]`
- `entity_id, feature_name, event_time`（`/explain`）
- `features`（可选）：只连接指定特征
- `use_event_as_of`（默认 `true`）：`false` 时退化为朴素 as-of join，**会泄漏**，仅用于对照

请求样例见 [`examples/`](examples/)，可用 `examples/requests.sh` 一键复现。

```bash
curl -s http://127.0.0.1:8000/health
curl -s -X POST http://127.0.0.1:8000/explain \
  -H 'Content-Type: application/json' \
  -d @examples/explain_late_revision.json
```

错误请求返回 HTTP 400 与中文错误信息（请求体上限 1 MiB）。

## 5. 泄漏对照实验

`leakage` 用固定种子生成 40 个实体 × 30 天的数据：每天每实体有一条
**准时入库但带噪声的初步值（v1）** 和一条 **72 小时后才入库的定稿值（v2，
恰为标签所用真值）**。对 320 个事件分别用两种 join 构造数据集，
按事件时刻做 70/30 时序切分，各训练一个纯 NumPy 逻辑回归。

实测（环境与数值见 [RUNLOG.md](RUNLOG.md)）：朴素 as-of join 的
时间外准确率比防泄漏 PIT join **虚高约 +0.27** —— 这部分"能力"
来自事件时刻尚不可见的事后定稿值，真实上线无法复现。

## 6. 项目结构

```
src/pitjoin/
  times.py       # 整数毫秒时间、ISO-8601 解析/格式化
  engine.py      # FeatureRecord/Event/FeatureStore、向量化选择、pit_join、选择依据
  synthetic.py   # 可手算的规范场景 + 可复现的大规模合成数据
  model.py       # 纯 NumPy 逻辑回归、标准化、时序切分
  serialize.py   # 证据/宽表 -> JSON（时间输出 ISO-8601）
  service.py     # 零依赖 HTTP 服务（/health /join /explain）
  cli.py         # demo / leakage / serve
examples/        # 请求样例（JSON）与 requests.sh
tests/           # pytest 自动化测试（69 个，覆盖率 99%）
```

引擎实现要点：特征按 `(entity_id, feature_name)` 分组，组内按
`(effective_time, ingest_time, version)` 预排序；as-of 用
`np.searchsorted`，可见性过滤与多版本裁决全部为 NumPy 向量化操作。

## 7. 测试

```bash
.venv/bin/pip install pytest pytest-cov
PYTHONPATH=src .venv/bin/python -m pytest --cov=pitjoin --cov-report=term-missing
```

覆盖三类验收情形（晚到特征、同时间多版本、缺特征）、毫秒级边界语义、
确定性/可复现性、实体间不串连、HTTP 端到端与输入校验、泄漏对照、模型训练。

## 8. 设计取舍说明

- **只做离线 join，不做在线服务状态更新**：特征库构造后不可变
  （`frozen=True` 数据类 + 元组），符合不可变原则，也保证结果可复现。
- 缺失值在宽表中以 `NaN` 表示，原因码仍可在 evidence 中区分
  `MISSING_KEY / ALL_FUTURE / ALL_LATE`，不会把"没有"和"当时不可见"混为一谈。
- 标签与特征使用同一潜在真值生成，是为了让"修订泄漏"可被干净地量化；
  这是合成数据的受控实验设定，不代表任何真实数据源。
