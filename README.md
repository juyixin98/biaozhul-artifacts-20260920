# 受限 OWL 类表达式后端服务 (miniowl)

纯后端服务，支持 OWL 的一个**受限子集**：类名、`ObjectIntersectionOf`（交集）、
`SubClassOf`（子类）、`EquivalentClasses`（等价）、`DisjointClasses`（互斥）、
`ClassAssertion`（类断言）。**不支持完整 OWL**——并集、补类、存在/全称限制、
基数约束、属性公理等一律以明确错误码 `UNSUPPORTED_CONSTRUCTOR` **拒绝**，绝不会
被当成普通标签静默吞掉。

技术栈：Python 3.12 · FastAPI · SQLite（标准库 `sqlite3`）· pytest。
全部数据均为本地合成夹具，无任何生产账号或外部业务依赖。

---

## 1. 它解决了什么（核心语义）

| 概念 | 本服务的判定 |
|------|--------------|
| **类不可满足 (class unsatisfiable)** | 假设一个实例属于该类，仅由 TBox 就推出互斥冲突 → 该类不可能有实例。**不要求**本体不一致。 |
| **本体不一致 (ontology inconsistent)** | 某个**已声明实例**（ABox）在互斥公理下无任何模型。与"类不可满足"严格区分、分别上报。 |
| **等价类归并** | 由饱和后的双向包含关系划分等价类；归并是只读聚合，**原始每条等价/子类声明及其公理 id 全部保留**。 |
| **实例互斥冲突** | 返回完整**冲突路径**：互斥公理 id、互斥类对、触发规则、以及两侧各自从哪条 `ClassAssertion` 推出的证明树。 |
| **不支持的构造** | 解析期即拒绝，带稳定错误码和精确位置（如 `axiom[1].sub`）。 |

**算法**：受限片段是 Horn 的，编译器把每条类表达式公理展开成扁平合取规则
（`C1 & … & Ck -> D`；互斥公理展开为 `… -> ⊥`），内核做带证明树的前向链
最小不动点饱和。对该子集，这既是可靠的也是完备的——不是硬编码演示。

**独立对照（oracle）**：另有一个**独立有限模型枚举器**，只共享纯语法 AST，
不导入内核/编译器，不做规则与不动点；它按集合论语义枚举全部 2^n 个类型标签，
逐标签校验。每次推理都自动与它交叉核对，参考答案不由被测核心自身产生。

---

## 2. 目录结构（多模块，职责分离）

```
src/min_iowl/
  lang/        规则语言：ast / parser(函数式+JSON) / compiler(→合取规则) / errors
  kernel/      推理内核：engine(前向链饱和+证明树) / equivalence(等价类归并+来源)
  oracle/      独立有限模型枚举器（参考实现，刻意不依赖内核）
  store/       SQLite 证据存储（本体/公理/推理运行/请求日志）
  service/     编排：编译→推理→等价→oracle 对照；证据 JSON 塑形
  api/         FastAPI 查询接口（请求关联、分阶段日志、失败/不确定分区）
  config.py    环境变量配置
scripts/       reason.py(CLI)、reproduce.sh(一键复现)
tests/         72 个测试（解析/编译/内核/oracle/交叉对照/存储/HTTP）
data/          合成夹具（JSON 与函数式语法）
examples/      curl 与 httpx 调用示例
results/       复现实跑产生的可复核结果（已随仓库保留一份）
docs/          ARCHITECTURE.md、REVIEW.md
```

---

## 3. 快速开始

```bash
python3 -m pip install -r requirements.lock   # 依赖锁定
make test                                     # 72 passed
make cov                                      # 覆盖率门槛 80%（实测 94.5%）
make run                                      # http://127.0.0.1:8000
```

不需要服务也能直接推理（最小可复核产物）：

```bash
PYTHONPATH=src python3 scripts/reason.py data/fixture_mutex_instance.json
PYTHONPATH=src python3 scripts/reason.py data/fixture_intersection_disjoint.json
PYTHONPATH=src python3 scripts/reason.py --functional data/fixture_hierarchy.owlf
```

一键完整复现（跑测试 + 覆盖率 + CLI + 真实起服务打正常/异常请求，结果落 `results/`）：

```bash
make reproduce        # 等价于 bash scripts/reproduce.sh
```

---

## 4. 接口与调用示例

### 创建本体（JSON 构造器形式）

```bash
curl -s -X POST http://127.0.0.1:8000/ontologies \
  -H 'Content-Type: application/json' \
  -H 'x-request-id: demo-001' \
  --data @data/fixture_intersection_disjoint.json
```

也接受**函数式语法文本**字段 `functional`，每行一条公理，例如
`SubClassOf(Dog Mammal)`、`EquivalentClasses(HomoSapiens Human Person)`、
`DisjointClasses(A B)`、`ClassAssertion(A x)`。

### 推理（含独立 oracle 对照与证据）

```bash
curl -s -X POST http://127.0.0.1:8000/ontologies/fixture-unsat-but-consistent/reason
```

响应分区（失败原因与不确定结论**单独成区**，不混入正向结果）：

- `state`：`ontology_inconsistent` 与 `unsatisfiable_classes` 两个独立标志；
- `results`：类层次、等价类（含 `direct_equivalence_axioms` 来源）、实例类型与证明树；
- `failures`：`UNSATISFIABLE_CLASS` / `MUTEX_INSTANCE_CONFLICT` / `ONTOLOGY_INCONSISTENT`，带 severity 与冲突路径；
- `uncertain`：如不一致时的 `EX_FALSO_QUALIFICATION`、oracle 跳过/分歧；
- `cross_check`：内核 vs 独立枚举器的逐项结论、枚举标签数、是否一致；
- `steps`：compile / saturate / equivalence_partition / oracle_cross_check 各步状态与位置；
- `engine_version`、`reasoning_ms`、`request_id`、`run_id`。

### 解释单个实例

```bash
curl -s -X POST \
  "http://127.0.0.1:8000/ontologies/fixture-mutex-instance/query?individual=mallory"
```

### 可解释性 / 请求关联

- 每个响应（含错误）都回 `x-request-id` 头与 `request_id` 体；可用
  `-H 'x-request-id: <自定义>'` 指定，否则自动生成。
- 同一 id 写入结构化日志行与 SQLite `request_log` 表，分阶段
  （received → compile_start → reasoned → completed）。
- 取回某次请求的处理轨迹：`GET /requests/{request_id}`。
- 历史推理运行：`GET /ontologies/{id}/runs`、`GET /runs/{run_id}`。

更多示例见 `examples/calls.sh` 与 `examples/client_demo.py`。

### 错误类别（测试按类别断言，而非只看非 200）

| HTTP | code | 触发 |
|------|------|------|
| 422 | `UNSUPPORTED_CONSTRUCTOR` | 使用了受限片段之外的 OWL 构造 |
| 422 | `MALFORMED_EXPRESSION` | 括号不匹配、操作数不足、未知类型 |
| 404 | `NOT_FOUND` / `UNKNOWN_INDIVIDUAL` | 本体/实例不存在 |
| 409 | `CONFLICT` | 本体 id 重复 |

---

## 5. 四个关键夹具（多层继承 / 等价环 / 交集 / 互斥）

| 文件 | 要点 | 实测结论 |
|------|------|----------|
| `data/fixture_hierarchy.json` / `.owlf` | 4 层继承 + 3 类等价环 | `rex` 推出 `Dog,Mammal,Animal,Organism`；`HomoSapiens≡Human≡Person` 合并且保留公理 `a005` |
| `data/fixture_intersection_disjoint.json` | 交集等价 + 互斥 | `Hermaphrodite` **不可满足**，但**本体仍一致**（无实例）；oracle 一致 |
| `data/fixture_mutex_instance.json` | 实例被两侧继承逼入互斥类 | 本体**不一致**；`mallory` 冲突路径两侧来源 `a006/a007`+互斥 `a005`；类本身都可满足 |
| `data/fixture_unsupported_rejected.json` | `ObjectSomeValuesFrom`/`ObjectUnionOf` | **422 UNSUPPORTED_CONSTRUCTOR**，位置 `axiom[1].sub` |

最小人工复核说明见 [`docs/REVIEW.md`](docs/REVIEW.md)，架构与完备性论证见
[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)。实跑结果保存在 `results/`。

## 6. 配置（环境变量，本地默认）

| 变量 | 默认 | 含义 |
|------|------|------|
| `MINIOWL_DB` | `data/miniowl.sqlite3` | SQLite 路径 |
| `MINIOWL_LOG_LEVEL` | `INFO` | 日志级别 |
| `MINIOWL_LOG_FILE` | 空（stderr） | 日志文件 |
| `MINIOWL_ORACLE_MAX_CLASSES` | `16` | 枚举器 2^n 安全上限 |
