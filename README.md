# pitjoin — 特征时间点连接（Point-in-Time Feature Join）

纯后端、纯本地依赖（Python + NumPy）的离线特征 join 服务。对每条脊柱行
`(entity_id, event_ts)`，按**双时态**语义选出该时刻真正可用的特征版本，
从机制上避免晚到数据与事后修订造成的训练泄漏。不下载任何外部模型或数据；
合成数据由固定随机种子生成，完全可复现。

## 核心语义

每条特征记录携带两个时间戳：

| 字段 | 含义 |
|---|---|
| `event_ts` | 事实在业务世界中生效的时刻（事件时间） |
| `ingest_ts` | 该版本落入特征仓库的时刻（入库/系统时间） |

对脊柱行 `(entity, T)` 与特征 `F`，在其全部版本上执行：

1. 保留 `event_ts <= T`（事实在 T 时刻已存在）；
2. 保留 `ingest_ts <= T`（该版本在 T 时刻**已入库** —— 排除晚到数据与事后修订）；
3. 幸存者中取 `event_ts` 最大者（最新事实）；并列时取 `ingest_ts` 最大者（当时可见的最新修订）。

无幸存版本时返回 `value=null` 并给出明确原因（无任何记录 / 事实尚未发生 /
版本均已晚到），绝不静默使用未来数据。每个 join 单元都输出选中依据：
选中的 `(event_ts, ingest_ts)`、候选数、因"事件时间在未来"和"入库晚到"
各自排除的版本数。

存储为**只追加（append-only）**：修正是同 `event_ts`、更大 `ingest_ts` 的
新记录，历史全量保留，任意历史时点视图可重现。

## 目录结构

```
pitjoin/
  records.py    # FeatureRecord / SpineRow 数据模型与校验
  store.py      # FeatureStore：只追加存储 + 按 (entity, feature) 排序的 NumPy 索引
  join.py       # point_in_time_join / join_as_matrix（含选中依据与排除统计）
  reference.py  # 朴素纯 Python 参考实现（用于交叉验证；支持泄漏模式对照）
  service.py    # stdlib http.server JSON 服务（/health /stats /ingest /join）
scripts/
  run_acceptance.py  # 验收场景：晚到特征 / 同刻多版本 / 缺特征，手算对照
  demo_leakage.py    # 泄漏演示：正确 join vs 泄漏 join 训练简单逻辑回归
samples/
  seed_records.json  # 服务启动种子数据（即验收场景数据）
  join_request.json  # /join 请求样例
tests/               # pytest：单元 + 交叉验证 + HTTP 集成测试（25 项）
```

## 快速开始

```bash
pip install -r requirements.txt   # 仅 numpy + pytest

# 1. 自动化测试
python3 -m pytest tests/ -q

# 2. 验收场景（手算对照，输出每个单元的选中依据）
python3 scripts/run_acceptance.py

# 3. 泄漏对比演示（合成数据 + NumPy 逻辑回归）
python3 scripts/demo_leakage.py

# 4. 启动服务并用样例请求调用
python3 -m pitjoin.service --port 8000 --data samples/seed_records.json
curl -s http://127.0.0.1:8000/health
curl -s -X POST http://127.0.0.1:8000/join \
  -H 'Content-Type: application/json' \
  -d @samples/join_request.json | python3 -m json.tool
```

## HTTP API

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 存活检查 |
| GET | `/stats` | 记录数、实体与特征列表 |
| POST | `/ingest` | `{"records": [{entity_id, feature, value, event_ts, ingest_ts}, ...]}`，只追加 |
| POST | `/join` | `{"spine": [{entity_id, event_ts}, ...], "features": [...]}` → 每单元返回 `value`、`selected_event_ts`、`selected_ingest_ts`、`reason` 及排除统计 |

请求体上限 10 MiB；参数错误返回 400，未知路径返回 404。

## 验收场景（scripts/run_acceptance.py，手算对照）

数据（节选）与手算结论：

- **晚到修订**：`u1/click_rate_7d` 在 `event_ts=200` 有两个版本
  （`ingest=215 → 0.20`，`ingest=260 → 0.99`）。脊柱 `T=250` 时 0.99 尚未入库，
  必须选 **0.20**；`T=350` 时修订已可见，选 **0.99**。
- **晚到事实**：`event_ts=300, ingest=400 → 0.30`。`T=350` 时事实虽"发生"
  但未入库，仍选旧值；`T=450` 时才选 **0.30**。
- **同刻多版本**：`u2/click_rate_7d` 两个版本同为 `event_ts=100`
  （`ingest=105 → 0.50`，`ingest=120 → 0.55`）。`T=110` 选 **0.50**，
  `T=130` 按 `ingest_ts` 决胜选 **0.55**。
- **缺特征**：`u2` 无 `avg_spend_30d` 任何记录 → `value=null`，
  原因 `MISSING: no records exist for (entity, feature)`。

## 泄漏演示（scripts/demo_leakage.py）

固定种子生成 4000 个事件：真实特征 `x ~ N(0,1)`，标签由 `x` 加噪生成；
一半记录随后被"回填作业"修订，修订值直接编码标签（模拟使用未来信息的
事后修订）。用 NumPy 手写逻辑回归分别在两种 join 结果上训练：

- 正确的 point-in-time join：只看到脊柱时刻已入库的原始值 → 诚实精度；
- 泄漏 join（忽略 `ingest_ts`）：选中编码标签的修订 → 精度虚高。

## 实际运行记录（如实记录）

环境：Python 3.12.3，NumPy 2.5.3，pytest 9.1.1，Linux。

| 命令 | 结果 |
|---|---|
| `python3 -m pytest tests/ -q` | **25 passed**（首轮 24 passed / 1 failed，见下） |
| `python3 scripts/run_acceptance.py` | **PASS**，10/10 单元与手算一致，exit=0 |
| `python3 scripts/demo_leakage.py` | **PASS**：诚实精度 0.7908 vs 泄漏精度 0.9067（虚高 +0.1158），exit=0 |
| `python3 -m pitjoin.service --port 8123 --data samples/seed_records.json` + curl | `/health`、`/stats`、`/join` 正常；缺字段请求正确返回 400 |

开发过程中出现并已修复的问题（未隐瞒）：

1. `pitjoin/__init__.py` 漏导出 `join_as_matrix`，首轮测试收集阶段 ImportError；已修复。
2. `test_late_arriving_fact_excluded` 初版断言写错：脊柱 350 时 `event_ts=300`
   的记录属于"入库晚到"（`rejected_late_ingest=1`）而非"事件时间在未来"；
   实现行为正确，修正的是测试断言。
3. `demo_leakage.py` 初版把 `naive_join` 的嵌套返回值当单层列表解包，
   `AttributeError`；已修复。
4. 演示脊柱时刻初版取 `t`（早于原始记录入库 `t+5`），会导致首波事件无可见
   特征；改为 `t+10`，位于原始入库（t+5）与泄漏修订（t+100）之间。

## 设计说明与边界

- 时间戳为整数（同一时钟/单位即可，如 epoch 秒）；两个时间过滤均为**含边界**（`<=`）。
- 索引按 `(event_ts, ingest_ts)` 字典序排序，查询用 `searchsorted` 定位前缀后从新到旧扫描，
  首个满足 `ingest_ts <= T` 者即答案（同时天然完成同刻 `ingest_ts` 决胜）。
- 正确性由 `tests/test_join.py::TestCrossValidation` 保证：固定种子随机数据上，
  NumPy 实现与朴素参考实现逐单元比对一致。
- 当前为单机内存实现，面向"验证核心机制"；生产化需替换为持久化存储与分区扫描，
  但双时态选择语义不变。
