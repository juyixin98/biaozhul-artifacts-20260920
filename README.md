# 关联规则提升度审计后端 (Association Rule Lift Audit)

基于**已挖掘频繁项集**生成关联规则，输出 **support / confidence / lift / leverage**，
并对每条规则给出可解释的审计结论（accepted / rejected / undefined / indeterminate / invalid）。
技术栈：Python 3.10+ · FastAPI · SQLite；全部数据为本地合成夹具，无外部账号、无真实业务数据。

## 1. 模块职责（非单文件脚本）

| 模块 | 真实职责 |
|---|---|
| `app/corpus.py` | **语料规范**：原始事务行解析、清洗、事务内去重、空行处理、规模边界校验 |
| `app/indices.py` | **索引与模型**：事务位集索引（交集计数）、频繁项集支持表 |
| `app/apriori.py` | **挖掘内核的生产者**：逐层 Apriori，物化频繁项集（也可直接喂入外部项集） |
| `app/mining.py` | **挖掘内核**：指标纯函数、规则二分枚举、阈值裁决、样本量/罕见事件警示 |
| `app/models.py` | 框架无关的领域模型与规范常量 |
| `app/validation.py` | **查询验证**：阈值范围、空前件/后件、前后件重叠的分类拒绝 |
| `app/diagnostics.py` | 结构化诊断：请求标识、关键状态、脱敏指纹 |
| `app/repository.py` | SQLite 持久化（数据集 / 事务 / 项集 / 审计运行记录） |
| `app/service.py` | 编排：入库→索引→挖掘→生成→裁决→落库，全程诊断 |
| `app/schemas.py` / `app/main.py` | Pydantic DTO 与 FastAPI 路由、异常映射、请求 ID 中间件 |
| `tests/` | 独立组织的测试、夹具、暴力 oracle、手算期望值 |
| `scripts/verify.py` | 可复用的三路独立验证脚本 |

## 2. 指标定义与边界语义

设 `n` 为事务数；所有计数均为**集合语义**——同一事务内重复出现的物品只计一次
（与 mlxtend 的 one-hot 语义一致）。

- `support(A→C) = |A∪C| / n`
- `confidence(A→C) = |A∪C| / |A|`
- `lift(A→C) = n·|A∪C| / (|A|·|C|) = supp(A∪C)/(supp(A)·supp(C))`
- `leverage(A→C) = supp(A∪C) − supp(A)·supp(C)`

**分母为零的显式语义（绝不产生 inf/NaN）：**

| 情形 | confidence | lift | leverage | 规则状态 |
|---|---|---|---|---|
| `|A| = 0` | `null`，未定义 | `null`，未定义 | 仍定义 | `undefined`，原因写明“antecedent support count is 0” |
| `|C| = 0`、`|A| > 0` | `0`（分子必为 0） | `null`，未定义 | 仍定义 | `undefined` |
| `|A∪C| = 0`（互斥） | `0` | `0` | 负值 | `defined`，正常裁决；零支持项集不会出现在频繁项集表中 |

**其他边界：**

- 空前件 / 空后件（含纯空白）：在系统边界以 `empty_antecedent` /
  `empty_consequent` 分类拒绝（HTTP 422），不进入挖掘。
- 前件与后件必须不相交：重叠以 `overlapping_antecedent_consequent` 拒绝，
  错误信息只给结构信息，**不回显物品名**。
- `min_support ∈ (0, 1]`：0 被拒绝——零支持项集按定义就不频繁，且会枚举出无意义的零计数规则。
- `min_confidence / min_lift / min_leverage` 超范围、类型错误、`max_rules ≤ 0` 均分类拒绝。
- **最小置信剪枝不漏规则**：未达阈值的规则仍保留在结果中，状态为 `rejected`
  并带具体原因（如 `confidence 0.750000 below min_confidence 0.900000`），而非静默删除。
- **高置信度不是因果**：`a→u` 置信度 1.0 但 lift 恰为 1（独立）。结果带
  `lift_not_causation` 警示；任何置信度/lift 都不声明因果关系。
- **样本量 / 罕见事件**：`n < 30` 给 `small_sample`；前件/后件/共现计数
  `< 5` 给 `rare_antecedent` / `rare_consequent` / `rare_rule`。指标定义且过阈值、
  但证据过稀（共现罕见或小样本）的规则判为 `indeterminate`（无法判定），
  而不是轻率 `accepted`。阈值可经环境变量调整。

lift 解读：`>1` 正关联；`=1` 独立；`<1` 拮抗。互斥项 `c,d`：lift=0、leverage=−6/25。

## 3. 安装与运行

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt        # 含固定版本的 mlxtend（交叉验证用）
.venv/bin/uvicorn app.main:app --reload          # http://127.0.0.1:8000 ，文档 /docs
```

环境变量（均有默认值）：`RULE_AUDIT_DB_PATH`、`RULE_AUDIT_SMALL_SAMPLE_N`、
`RULE_AUDIT_RARE_EVENT_COUNT`、`RULE_AUDIT_MAX_TRANSACTIONS`、
`RULE_AUDIT_MAX_ITEMS_PER_TXN`、`RULE_AUDIT_REDACT_PII`。

### API 速览

```bash
# 入库（合成数据），同时挖掘频繁项集
curl -s -X POST localhost:8000/api/datasets -H 'content-type: application/json' -d '{
  "name":"demo",
  "min_support":0.2,
  "transactions":[["u","a","b","c"],["u","a","b","c"],["u","a","b","d"],
                  ["u","a","d","e"],["u","c","e"]]
}'

# 生成并审计规则（不静默丢弃任何候选）
curl -s -X POST localhost:8000/api/datasets/demo/rules -H 'content-type: application/json' \
  -d '{"min_confidence":0.0}' | python3 -m json.tool
```

每个响应/错误都带 `request_id`（也接受入站头 `x-request-id` 做关联）。

## 4. 测试与验证（断言具体结果与失败类别）

```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q                     # 43 个测试
.venv/bin/python scripts/verify.py                # 三路独立验证，非 0 退出码即失败
```

**参考答案不依赖被测内核自身生成**，由三路独立证据构成：

1. **手算分数** `tests/fixtures/expected_metrics.json`：在纸面按
   `(union, antecedent, consequent)` 列联计数算出的精确分数（如 `a→b`：
   conf=3/4，lift=5/4，leverage=3/25），测试用 `fractions.Fraction` 精确比较。
2. **朴素暴力 oracle** `tests/reference.py`：只做原始行集合遍历，**不导入**
   `app.mining/apriori/indices`，独立重算每个指标。
3. **第三方库 mlxtend**：项集族、support、confidence、lift 逐项对照；
   并专门验证“事务内重复物品不抬高支持度”。缺失时该检查标记为 **SKIPPED，不计通过**。

覆盖案例：普遍项（`u` 5/5）、互斥项（`c,d` 共现 0）、稀有组合（`a,e` 共现 1）、
最小置信剪枝边界（`u→a` 恰为 0.8 不被剪，`a→b` 0.75 被剪且原因明确）。
失败类别用 `InvalidReason` / `RuleStatus` 枚举断言，而非只检查“接口能调用”。

## 5. 诊断与脱敏

所有接受/拒绝/无法判定都写结构化记录（阶段、状态、关键计数），持久化到
`audit_runs` 表。默认 `RULE_AUDIT_REDACT_PII=true`：日志与诊断中物品名一律
替换为 `{size, itemset_sha256_10}` 不可逆指纹；重叠校验等错误信息不回显物品值。

## 6. 无法执行 / 未纳入的检查（如实单列，不写成已通过）

- **超大规模性能压测**：仅实现位集计数与逐层 Apriori，未对百万级事务做基准测试，
  不声称性能指标。
- **显著性检验**：未计算 lift 的卡方/Fisher 显著性；`indeterminate` 是基于
  样本量/罕见计数的工程警示，不是统计假设检验结论。
- **认证鉴权 / 多租户授权 / 限流**：本地合成审计后端，未实现，亦无真实敏感数据接入。
- 若运行环境未安装 `mlxtend`，`scripts/verify.py` 第 5 组与 pytest 中对应用例
  报告 SKIPPED（明确“未执行”），其余检查照常运行。
