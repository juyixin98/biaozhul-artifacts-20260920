# 边界语义说明

本文档固定关联规则提升度审计后端的边界语义。测试（`tests/`）与验证脚本
（`scripts/verify.py`）断言的正是这些语义。

## 1. 语料规范（app/corpus）

- 一个事务是项的**集合**。同一事务内的重复项在入库前去重——与主流支持度库
  （mlxtend 的 one-hot 编码、efficient-apriori 的 frozenset 语义）一致：
  一个事务对任何项集的支持计数最多贡献 1。
- 项为去空白后的非空字符串，长度上限 256；事务 id 在语料内唯一。
- 去重后被清空的事务、空语料、重复事务 id 均以稳定类别拒绝：
  `EMPTY_TRANSACTION`、`EMPTY_CORPUS`、`DUPLICATE_TRANSACTION_ID`、
  `EMPTY_ITEM`、`ITEM_TOO_LONG`、`EMPTY_TRANSACTION_ID`。
- 入库响应报告 `n_duplicate_items_removed`，去重行为可审计。

## 2. 指标定义（app/mining/rules.py）

N 为事务总数，count(X) 为包含 X 的事务数：

| 指标 | 公式 | 分母为零时 |
|---|---|---|
| support | count(X∪Y) / N | 不可能（N ≥ 1 已校验） |
| confidence | count(X∪Y) / count(X) | count(X)=0 → **未定义（null）** |
| lift | confidence / (count(Y)/N) | count(Y)=0 或 confidence 未定义 → **未定义（null）** |
| leverage | support(X∪Y) − support(X)·support(Y) | 总有定义 |

- **零分母 = 未定义**，输出 JSON `null`，绝不是 0 或 NaN；诊断日志记录
  `decision=undecidable reason=UNDEFINED_ZERO_DENOMINATOR` 及关键计数。
- 每个指标都是精确整数的单次除法，结果与 `float(Fraction(...))` 逐位一致，
  使测试能对独立参考实现断言精确相等。

## 3. 规则查询的范围拒绝（app/validation）

- **空前件拒绝**（`EMPTY_ANTECEDENT`）：空前件下置信度退化为后件流行度，
  不属于本审计范围。
- **空后件拒绝**（`EMPTY_CONSEQUENT`）：空后件不构成任何预测。
- 前件与后件相交拒绝（`OVERLAPPING_SIDES`）。
- 阈值范围：`min_support`、`min_confidence` ∈ (0, 1]，`min_lift` > 0；
  越界以 `THRESHOLD_OUT_OF_RANGE` 拒绝。

## 4. 高置信度 ≠ 因果

- 规则筛选支持 `min_confidence` 与 `min_lift` 双条件；仅按置信度排序会把
  "后件本来就普遍"的规则误推到前面。
- `confidence ≥ 0.8` 且 `lift ≤ 1` 的规则挂 `HIGH_CONFIDENCE_LOW_LIFT`
  警示：高置信度由后件流行度解释，不能读作关联、更不能读作因果。
- 后件支持度 ≥ 0.9（可配）挂 `UBIQUITOUS_CONSEQUENT`。
- 置信度阈值 0.8 为常量 `HIGH_CONFIDENCE_THRESHOLD`；低于它的低 lift
  不挂此警示（避免噪声）。

## 5. 样本量与罕见事件警示

- `SMALL_SAMPLE`：语料事务数 < 30（可配 `small_sample_threshold`）。
- `RARE_EVENT`：规则联合支持计数 < 5（可配 `rare_event_count_threshold`）。
  2 次观测上的 100% 置信度不可外推。
- 警示写入每条规则结果的 `warnings` 数组，随 API 与存储一起返回。

## 6. 最小置信度剪枝的完备性

对固定项集 Z，规则 X→Y（X∪Y=Z）的置信度为 count(Z)/count(X)。缩小后件
会扩大前件、只能降低 count(X)、从而提高置信度。因此某后件不通过
min_confidence 时，其任何超集后件也不可能通过——只对通过的后件做
Apriori 合并是**完备**的。`tests/test_pruning_completeness.py` 在全部夹具、
多组阈值下将剪枝生成器与穷举枚举及独立 Fraction 参考实现逐一比对。

## 7. 诊断与脱敏

- 每个请求有 `X-Request-ID`（客户端可指定，否则服务端生成），写入每条
  决策日志与响应体 `request_id` 字段。
- 决策日志格式：`decision=accepted|rejected|undecidable reason=<类别>
  request_id=<id> record_id=<corpus:id> state={关键计数/阈值}`。
- 项值可能敏感（如医疗编码），日志默认只打印 `item#<sha256前12位>`；
  可配 `mask_items_in_logs=False` 关闭（仅建议本地调试）。

## 8. 本环境无法执行的检查（未通过 ≠ 已通过）

以下检查在 `scripts/verify.py` 中以 `NOT EXECUTED` 单列，不计入通过数：

- 大语料性能检查（本环境无生产规模数据）。
- SQLite 存储的并发写行为（本交付为单进程范围）。
