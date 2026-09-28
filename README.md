# 类型化查询：静态成本估计与运行时预算网关

在查询执行前做**静态成本估计**，再用一个**预算网关**决定放行；运行中逐解析器
**实际扣减**，一旦超限立即**取消未完成解析器**并返回带标注的**部分结果**。

技术栈：TypeScript（strict）· Node.js 22（内置 `node:sqlite`，无需原生编译）·
Fastify 5 · Vitest。全部数据来自本地合成夹具（内存 SQLite），无外部账号。

## 它解决什么

- 成本只由三件事决定，**不看查询字符数**（查询是结构化 AST，执行内核从结构上
  无法接触任何源码文本长度）：
  1. **片段展开**：每个 spread / include 按出现次数展开为独立字段子树，
     不记忆化、不去重——重复片段重复计费，无法绕过预算；
  2. **列表基数**：未知列表规模使用查询里声明的上界 `declaredUpperBound`，
     进入列表时父成本按基数相乘；
  3. **字段倍率**：标量字段按 schema 的 `multiplier` 计费（重字段更贵）。
- 变量在任何执行前先完成**类型校验**（含过滤参数与目标字段的类型兼容）。
- 静态估计超过预算 → 直接拒绝，**永不加载状态、永不启动解析器**。
- 估计放行但实际夹具更大（声明上界偏乐观）→ 运行中扣减到超限时取消，
  保留前缀部分结果，未完成解析器在成本树上标注为 `aborted`。

## 模块边界

```
contracts/   数据与错误契约（AST、schema、成本树、结果、四类错误）
resolution/  契约解析：形状解析 → 变量类型校验 → 片段展开(含循环检测)
             → 选择集/类型/别名/上界校验 → 变量参数校验
kernel/      执行内核：静态估计器、预算账本 RunContext、引擎(网关+运行+取消)
state/       状态适配：EntityStore 接口 + node:sqlite 实现（参数全部绑定）
fixtures/    本地合成 schema 与种子数据（期望值据此手工计算）
diagnostics/ 运行日志（runId / 分阶段中间状态 / 判断理由 / 扣减台账）+ 成本树渲染
http/        Fastify 诊断接口
```

模块间只通过 `contracts/` 的类型与 `EntityStore` 接口通信；适配器异常在引擎
边界统一转译为 `COMPUTATION_FAILED`，“对象不存在”经返回 `null` 表达为
`STATE_CONFLICT`。

## 快速开始

```bash
node --version        # 需要 >= 22.5（用到内置 node:sqlite）
npm install
npm test              # 实际执行 36 个测试并报告结果
npm run typecheck     # tsc --noEmit，strict 下零错误
npm run demo          # 五个验证场景的本地演示，打印逐字段成本树与台账
npm start             # http://127.0.0.1:3000 （PORT 可改）
```

## HTTP 接口

统一包络 `{ success, data, error, meta }`。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 存活检查 |
| POST | `/query/explain` | 只做静态估计与网关判定，**不接触状态层**，返回逐字段成本树 |
| POST | `/query` | 执行；静态超限 413，运行中超限返回 200 且 `status=partial` |
| GET | `/runs` | 最近运行编号列表 |
| GET | `/runs/:id` | 单次运行的完整重放记录（阶段、理由、台账） |

`POST` 体：`{ "budget": <number>, "query": <Query AST> }`。

可直接复现（服务启动后）：

```bash
curl -s -X POST localhost:3000/query/explain -H 'content-type: application/json' -d '{
  "budget": 100,
  "query": { "root": "Org", "rootId": 1, "fields": [
    { "kind": "field", "alias": "name", "field": "name" },
    { "kind": "list", "alias": "members", "relation": "members",
      "declaredUpperBound": 2,
      "children": [{ "kind": "field", "alias": "name", "field": "name" }] }
  ]}
}'
```

更多场景见 [`examples/requests.json`](examples/requests.json)。

## 查询 AST（摘要）

```jsonc
{
  "root": "Org",
  "rootId": 1,
  "vars": [{ "name": "orgId", "type": "int", "value": 1 }],   // 先类型校验
  "fragments": ["orgSummary"],                                // 顶层 include，可重复
  "fields": [
    { "kind": "field", "alias": "n", "field": "name" },
    { "kind": "spread", "fragment": "taskCore" },             // 片段展开（无别名）
    { "kind": "list", "alias": "members", "relation": "members",
      "declaredUpperBound": 2,                               // 未知规模的声明上界
      "args": [{ "var": "orgId", "op": "eq" }],
      "children": [ /* 必须非空 */ ] }
  ]
}
```

## 成本如何计算

叶子的相对根单位贡献：

```
unitContribution(叶子) = 字段 multiplier
                         × 从根到该叶子经过的所有列表 declaredUpperBound 之积
estimatedTotal         = 所有叶子 unitContribution 之和
```

- 片段展开先于估计：同一字段被展开两次就是两片叶子、各计一次。
- 运行时按**单个解析器**扣减：标量按倍率逐字段扣；列表解析出真实基数后
  逐元素逐叶子扣。已发生的真实基数回填到成本树（`actualCardinality` /
  `actualSubtree`）。实际基数超过声明上界时产生 `DECLARED_BOUND_EXCEEDED`
  告警——这正是“估计低于实际夹具”的入口。
- 查询长度在任何路径都不可达；增加空白、冗长的未知键不会改变估计
  （测试 `成本不看查询字符数` 明确断言）。

## 错误语义（四类失败必须可区分）

| category | 何时产生 | HTTP | 典型 code |
|---|---|---|---|
| `INPUT_ERROR` | 结构/类型/未知名/重复别名/空选择/非法上界/片段循环/变量类型不符 | 400 | `MALFORMED_QUERY` `UNKNOWN_TYPE` `UNKNOWN_FIELD` `UNKNOWN_RELATION` `UNKNOWN_FRAGMENT` `INLINE_FRAGMENT_FORBIDDEN` `DUPLICATE_ALIAS` `EMPTY_SELECTION` `FRAGMENT_CYCLE` `DUPLICATE_VAR` `UNKNOWN_VAR` `VAR_TYPE_MISMATCH` `VAR_ARG_TYPE_MISMATCH` `INVALID_BOUND` |
| `STATE_CONFLICT` | 通过校验后，数据前置条件不成立 | 409 | `ROOT_NOT_FOUND` |
| `RESOURCE_EXHAUSTED` | 静态估计超预算，或运行中实际成本超预算 | 413（静态）/ 200+partial（运行时） | `BUDGET_EXCEEDED_STATIC` `BUDGET_EXCEEDED_RUNTIME` |
| `COMPUTATION_FAILED` | 状态适配器等基础设施抛错 | 500 | `STORE_FAILURE` |

运行结果 `status`：

- `ok`：完整执行；
- `partial`：运行中预算超限。`data` 是前缀部分结果（已入列但未完成的元素
  保留为空对象），`abort.at` 给出触发取消的字段路径，成本树中未完成的
  解析器标 `runtimeStatus: "aborted"`，`ledger` 末步 `rejected: true`；
- `rejected_static`：静态网关拒绝，`consumed=0`、`data=null`，成本树保持
  `pending`（证明没有任何解析器运行）；
- `failed`：携带上述某类 `error`。

## 验证过程（`npm run demo` 与测试保留）

| 场景 | 构造 | 断言要点 |
|---|---|---|
| A 短而深 | `members(声明1/实际2)→tasks(2)→comments→replies×3`，估计 16 / 预算 20 | 在 `replies2.text` 深层取消并向上传播；成员1 完整、成员2 前缀保留；输出完整逐字段成本树 |
| B 重复别名 | 同层两个 `alias:"n"` | `INPUT_ERROR/DUPLICATE_ALIAS` |
| C 循环片段 | inline 片段 `loopA→loopB→loopA` | `INPUT_ERROR/FRAGMENT_CYCLE`，报告环，不栈溢出 |
| D 估计低于实际 | `orgTasks` 声明上界 2、实际 6，估计 5 恰好放行 | 运行到第 5 个任务取消；告警标注 2<6；台账末步 rejected；其后的 `plan` 解析器 aborted |
| E 静态网关 | 估计 8 > 预算 7 | `rejected_static`，`consumed=0` |

另含“重复片段在两个分支各计一份、运行时逐份扣减”的测试，证明重复展开
不能绕过预算。

### 参考答案独立于被测核心

测试里的期望成本/结果数字都按 `src/fixtures/seed.ts` 的固定常量**手工计算**
（如 `2*2*(1+3)=16`、实际 6 个任务按 `edge.position` 的确定顺序），
不是调用执行内核生成后再自比。`tests/resolution.test.ts` 中的 `staticCost`
是测试独立书写的静态管线，与内核的 `resolveStatic` 不共享实现。

## 诊断日志与问题重放

每次运行有 `runId`（如 `run_mujx7wme_1_0bmpdp`）。`GET /runs/:id` 返回：

- `phases`：`parse → validate-vars → expand-fragments → validate-selection →
  estimate-static → gateway-accepted → execute（→ aborted）` 各阶段时间点；
- `estimatedTotal` / `decisionReason`：网关判断及理由；
- `consumed` / `ledger`：逐步扣减的 `{at, amount, consumedAfter, remaining,
  rejected}`，末步即取消点；
- `error` / `finalStatus`。

默认服务运行额外把 JSONL 追加到 `logs/runs.jsonl`（演示与测试不落盘）。

## 测试

```bash
npm test
# Test Files  3 passed (3)
#      Tests  36 passed (36)
```

- `tests/resolution.test.ts`：变量类型校验、片段展开/循环、别名/上界/结构
  校验、成本三要素（17 项）；
- `tests/engine.test.ts`：网关、运行时扣减、取消传播、部分结果、实际成本树、
  四类失败区分（11 项）；
- `tests/http.test.ts`：状态码语义与诊断接口（8 项）。
