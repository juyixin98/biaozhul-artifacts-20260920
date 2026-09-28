# 架构与推理内核说明

## 1. 分层与数据流

```
HTTP (JSON 构造器 / 函数式文本)
        │  min_iowl.api
        ▼
parser ──► AST (lang/ast.py, 全部 frozen)
        │  明确拒绝：不支持构造 → LangError(UNSUPPORTED_CONSTRUCTOR)
        ▼
compiler ──► 扁平合取规则 Rule(body, head|⊥) + 基事实 Fact，均带 source_axiom
        ▼
engine.saturate ──► 前向链最小不动点；每步保留证明树 BaseNode/DerivedNode
        ├─ ABox：每个已声明实例独立饱和 → 冲突即"本体不一致"
        └─ TBox：对每个类假设一个虚拟实例(HYPOTHESIS) → 出 ⊥ 即"类不可满足"
        ▼
equivalence ──► 基于双向严格包含的并查集分组；归并只聚合、不删原声明
        ▼
service.oracle_check ──► 独立有限模型枚举器交叉核对（不共享推理代码）
        ▼
explain.shape_result ──► 证据 JSON（正向结果 / failures / uncertain 分区）
        ▼
store (SQLite)：ontologies · axioms · reasoning_runs · request_log
```

## 2. 为什么前向链对该片段是完备的

受限片段内，每个类表达式都是**具名类的合取**（交集可嵌套、扁平化）。
每条公理因此可写成 Horn 规则：

- `SubClassOf(C1⊗…⊗Ck, D1⊗…⊗Dm)` → 对每个 `Dj` 一条 `C1,…,Ck -> Dj`；
- `EquivalentClasses(...)` → 操作数两两之间双向子类规则；
- `DisjointClasses(...)` → 每一对展开一条 `… -> ⊥`；
- `ClassAssertion(x, C)` → 每个合取一条基事实。

Horn 规则集的最小不动点恰好是其逻辑后承的最小 Herbrand 模型；对单谓词
（一元类）情形，前向链饱和枚举了所有且仅有的可证原子。故在该片段内，
能推出的类型/包含/互斥冲突不会遗漏，也不会多出（每条推导都有规则与前提证明）。

不可满足与不一致的判定方式刻意分离：

- **类可满足性**：向工作集注入一个标记为 `HYPOTHESIS` 的虚拟实例属于 C，
  仅用 TBox 规则饱和。若互斥规则触发，C 不可满足。该虚拟事实**不是** ABox，
  因此不影响"本体是否有模型"。
- **本体一致性**：只对 `ClassAssertion` 声明的真实实例饱和；任一实例触发
  ⊥ 规则则 ABox 无模型，本体不一致。

由此"存在不可满足类但本体一致"（夹具2）与"本体不一致但无类被判定不可满足"
（夹具3，两个父类各自都有模型，仅某实例同时属于二者）都能被正确区分。

## 3. 证明树与冲突路径

- 每个推导出的类型持一棵证明树：叶是 `fact`（指向某条 `ClassAssertion`）或
  `hypothesis`；内部节点记录触发的 `rule_id`、`rule_kind` 与 `source_axiom`。
- 互斥规则触发时生成 `ConflictPath`：互斥公理 id、互斥类对、触发规则 id，
  以及对**每个**互斥操作数的一棵证明树。`sources` 汇总路径上的全部原始
  公理 id，可直接回溯到用户声明，做到"实例属于互斥类给冲突路径"。

## 4. 等价类归并不丢来源

- 并查集按饱和后 `D∈supers(C) 且 C∈supers(D)` 合并，既能捕获直接
  `EquivalentClasses`，也能捕获由子类环/交集诱导的等价。
- 合并不改写存储：所有公理仍以各自 id 单独存于 `axioms` 表。每个等价组返回
  `supporting_axioms`（涉及成员的全部公理）与 `direct_equivalence_axioms`
  （仅直接等价声明）。等价环 `A≡B`、`B≡C` 合并为 `{A,B,C}` 时两条来源都保留。

## 5. 独立有限模型枚举器（oracle）为何独立、为何够用

独立性（有测试强制保证，见 `tests/test_oracle.py`）：

- 仅 `from ..lang import ast`，不 import kernel/compiler/service；
- 不构造规则、不跑不动点；只把表达式**纯语法地**拆成具名类集合，
  然后按集合论语义暴力枚举。

完备性依据：片段内没有属性、基数、`oneOf`、（不）相等、存在限制；每条公理都
是对**单个域元素类型标签**的布尔全称约束，且类的外延允许为空。因此：

- 类可满足性在大小为 1 的域上即可见证/反驳；
- 实例之间不交互，本体一致性可逐实例独立判断。

枚举器遍历全部 `2^|类|` 个标签（合成夹具很小，默认上限 16 个类、65536 标签）：
分别求每个类的见证标签、每个实例可满足的标签集合与其交集（谨慎后承类型），
以及具名类间的包含关系。它与内核的任何分歧都会进入响应的 `uncertain` 区
（`CROSS_CHECK_MISMATCH`），而不是被悄悄忽略。

## 6. 证据存储与可解释性

SQLite 四张表（见 `store/evidence.py`）：

- `ontologies` / `axioms`：本体与每条公理原文（函数式）、稳定 id；
- `reasoning_runs`：引擎版本、耗时、不一致标志、不可满足类列表、完整证据 JSON；
- `request_log`：以 `request_id` 为键的分阶段事件（方法、路径、状态、详情）。

API 响应、日志行、库内事件用同一个 `request_id` 串联（可由请求头
`x-request-id` 指定）。关键处理步骤在响应 `steps` 与 `request_log` 中均可查。

## 7. 明确的范围边界

拒绝清单见 `lang/errors.py: UNSUPPORTED_CONSTRUCTORS`（并、补、存在/全称、
基数、数据域、属性链、各类属性特性、same/different individual 等）。未知
token 也不落成类名：解析器区分公理关键字、交集构造器与普通名字，未识别者
报 `MALFORMED_EXPRESSION`。"不支持"永远是显式错误，而非普通标签。
