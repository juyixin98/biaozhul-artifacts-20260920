# 最小可复核说明（无需相信代码，可手算核对）

本文件给出几个**可以靠纸笔独立核对**的结论，并指向仓库里已实跑保留的结果文件。
参考答案（oracle）由独立枚举器给出，**不是被测内核自己生成**；测试同时断言
具体结论与失败类别。

- 测试：`results/test-output.txt`（72 passed）；覆盖率 `results/coverage.txt`（94.5%，门槛 80%）
- CLI 结果：`results/cli-*.txt` / `results/cli-*.json`
- 真实 HTTP 结果：`results/http-*.json`；服务端日志 `results/server.log`
- 复现命令：`make reproduce`

---

## A. 互斥的真值表（2 个类，4 个标签，逐个检查）

公理 `DisjointClasses(A B)`。一个域元素的类型标签是 {A,B} 的子集：

| 标签 | A 成立 | B 成立 | 是否违反互斥 |
|------|:---:|:---:|:---:|
| ∅ | 否 | 否 | 合法 |
| {A} | 是 | 否 | 合法 |
| {B} | 否 | 是 | 合法 |
| {A,B} | 是 | 是 | **非法** |

手算结论：A、B 各自可满足（见证分别 {A}、{B}）；**没有实例时本体一致**；
若某实例同时被断言 A、B，则无合法标签 → **本体不一致**。

再加 `C ⊑ A`、`C ⊑ B`：任何含 C 的合法标签都必须同时含 A、B（两条子类），
而上表说明含 {A,B} 的标签非法。故**含 C 的合法标签不存在 → C 不可满足**。
注意此时若没有任何 C 的实例，本体仍有模型（例如全空标签），**本体一致**。
这正是"类不可满足 ≠ 本体不一致"，对应夹具 `fixture_intersection_disjoint.json`
（C = Hermaphrodite），`labels_explored = 2^6 = 64`。

## B. 多层继承的推导链（正向链，逐步）

夹具 `fixture_hierarchy.json`：`Dog ⊑ Mammal ⊑ Animal ⊑ Organism`，
实例断言 `rex : Dog`（公理 a007）。

```
a007 得 Dog
a001 Dog       → Mammal
a002 Mammal    → Animal
a003 Animal    → Organism
```
∴ rex 类型恰为 {Dog, Mammal, Animal, Organism}。oracle 在全部
`2^8 = 256` 个标签上取交集，得到同一集合。见
`results/http-reason-hierarchy.json`。

## C. 等价环（来源不丢）

`EquivalentClasses(HomoSapiens Human Person)`（公理 a005）展开成对双向规则；
`Person(alice)`（a008）经 `Person→Human→Mammal→Animal→Organism` 传播。
归并结果 {HomoSapiens, Human, Person} 的 `direct_equivalence_axioms = ["a005"]`，
原始公理仍单独存于 `axioms` 表——归并是只读聚合。环上三条类两两双向包含，
手算即知 alice 同时属三者（见 `results/cli-fixture_hierarchy.txt`）。

## D. 交集语义

`SubClassOf(Parent ⊓ Male, Father)` 编译为**单条**规则 `Parent, Male → Father`
（两个合取缺一不可）；`ClassAssertion(Parent ⊓ Male, bob)` 给出两条基事实。
仅当两条都在工作集，规则才触发 → bob 得 Father。测试
`test_intersection_requires_all_conjuncts_to_fire` 反例验证：只有 Male 时不触发。

## E. 实例互斥冲突路径（夹具 mutex）

`fixture_mutex_instance.json` 中 `mallory` 被 a006 断言为 Mother、a007 断言为
Father；`Mother ⊑ Parent ⊓ Female`（a001，展开出 Mother→Female 等规则），
`Father ⊑ Parent ⊓ Male`（a003，展开出 Father→Male 等规则），
`Disjoint(Male,Female)`（a005）。

```
a007 Father ──(a003 展开, r0005)──► Male   ┐
                                          ├─ 同时成立 → 命中互斥规则(r0007, 来自 a005)
a006 Mother ──(a001 展开, r0002)──► Female ┘
```

（a002 `Mother⊑Female`、a004 `Father⊑Male` 提供了等价的冗余推导；最小证明树
选取最先触发的交集展开规则 r0002/r0005。）

响应 `failures` 中：

- `MUTEX_INSTANCE_CONFLICT`（INDIVIDUAL_LEVEL，individual=mallory）
  - `disjoint_pair=[Male,Female]`
  - `conflict_path` 给出 Male、Female 两棵证明树
  - `sources=[a007,a003,a006,a001,a005]`（两侧推导链 + 互斥声明）
- `ONTOLOGY_INCONSISTENT`（ONTOLOGY_LEVEL）
- `uncertain` 中 `EX_FALSO_QUALIFICATION`（不一致下只列规则可证类型）

而 Father、Mother **各自可满足**（枚举器找得到只含 Male 侧或 Female 侧的标签），
所以 `unsatisfiable_classes=[]` 而 `ontology_inconsistent=true`——与夹具2恰好相反，
两个状态被清楚区分。见 `results/http-reason-mutex.json` 与
`results/http-query-mallory.json`。

## F. 不支持构造被明确拒绝

`fixture_unsupported_rejected.json` 含 `ObjectSomeValuesFrom` 与 `ObjectUnionOf`。
实跑返回（见 `results/http-err-unsupported.json`）：

```json
HTTP 422
{"error":{"code":"UNSUPPORTED_CONSTRUCTOR",
          "message":"constructor 'ObjectSomeValuesFrom' is not supported in the restricted fragment",
          "position":"axiom[1].sub"}}
```

属性名 `hasPet` 不会变成类标签（有测试断言这一点）。

## G. 失败类别（测试按 code 断言）

| 场景 | HTTP | error.code | 结果文件 |
|------|:---:|------|------|
| 不支持构造 | 422 | UNSUPPORTED_CONSTRUCTOR | `results/http-err-unsupported.json` |
| 本体不存在 | 404 | NOT_FOUND | `results/http-err-unknown-onto.json` |
| 实例不存在 | 404 | UNKNOWN_INDIVIDUAL | `results/http-err-unknown-individual.json` |
| id 重复 | 409 | CONFLICT | （复现脚本打印 `duplicate-create status=409`） |

## H. 请求可解释性

同一次请求可用同一 `x-request-id` 串起响应头/体、日志行、`request_log` 表。
`results/http-request-trace.json` 展示了 `repro-mutex` 的分阶段轨迹：
`received → compile_start → reasoned(200, inconsistent=true) → completed`，
每行带方法、路径、时间戳与处理位置。
