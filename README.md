# 类型化查询的静态成本估计与运行时预算网关

一个从空目录实现的、可运行的最小系统：对一个类型化查询语言做**静态成本估计**（静态预算门）
与**运行时逐笔扣减的预算网关**（超限取消未完成解析器并保留部分结果）。

- **技术栈**：TypeScript（严格模式）· Node.js 22（内置 `node:sqlite`，无需原生编译）· Fastify 5
- **数据**：全部为本地合成夹具（`src/fixtures/`），无外部账号、无真实业务数据
- **测试**：`node:test` 内置运行器，独立参考答案（不抄被测核心），断言具体数值与失败类别

---

## 1. 它解决什么问题

一个嵌套列表查询在真实数据上的解析代价会随**列表基数**和**字段倍率**放大：

```
users { posts { tags { weight } } }
```

静态阶段不知道真实行数，只能依据 schema 中声明的**基数上界**给出保守估计；
但真实夹具可能超过声明上界。因此需要两道门：

1. **静态预算门（STATIC_GATE）**：估计成本 > 预算 → 执行前直接拒绝（`RESOURCE_EXHAUSTED`）。
2. **运行时预算门（执行内核）**：静态放行后，执行时按**真实基数**逐字段、逐列表元素扣减；
   一旦某笔扣减会使累计成本超过预算，立即取消当前未完成的解析器，
   返回 `status=PARTIAL`、已完成数据前缀、逐字段实际成本树和取消标注。

**成本单位不是查询字符数。** 注释、空白、字段名长短都不影响成本（有专门测试锁定这一点）。

---

## 2. 成本模型

字段成本 = 字段自身 **倍率（multiplier）** + 所有子选择成本之和。

列表字段：

```
cost(listField) = multiplier + 实际元素数 × Σ cost(每个元素的子选择)     # 运行时
cost(listField) = multiplier + 声明上界   × Σ cost(每个元素的子选择)     # 静态估计
```

- 未知列表规模一律使用 schema 里的 **`declaredUpperBound`**；估计器绝不查询真实行数。
- 本夹具刻意让真实基数高于声明上界，用于复现“估计低于实际”：

| 列表 | 声明上界 | 夹具真实值 |
| --- | --- | --- |
| `users` | 10 | 12 |
| `User.posts` | 5 | 6 |
| `Post.tags` | 4 | 5 |

字段倍率举例：`role=4`、`title=2`、`weight=3`，其余多为 `1`（见 `src/fixtures/schemaDef.ts`）。

**片段（fragment）计费**：片段展开点自身 0 成本，但其下字段完整计费；
同一个片段在查询中出现 N 次（包括别名下重复），就产生 N 棵独立计费子树——
**片段重复不能绕过预算**。

### 示例：S4 查询

```
{ users { id posts { id tags { name weight } } } }
```

- 元素成本：tag = `name1 + weight3` = 4；tags = `1 + 基数×4`；post = `id1 + tags`；user = `id1 + posts`
- 静态（10/5/4）：`1 + 10×(1 + 1 + 5×(1 + 1 + 4×4))` = **921**
- 实际（12/6/5）：**1609**
- 预算设为 **1200**：静态放行（921 ≤ 1200），运行时在
  `$.users.8.posts.5.tags.3.weight` 处扣第 1201 个成本单位时取消，返回 PARTIAL。

---

## 3. 模块边界

```
src/
  contract/types.ts       跨模块数据契约：schema / 查询AST / 成本树 / 执行结果
  errors/DomainError.ts   四类可区分失败的领域错误
  schema/schema.ts        类型化模型：字段、倍率、列表声明上界、片段声明
  query/
    parser.ts             词法 + 递归下降语法解析（只结构化，不校验类型、不计成本）
    validator.ts          变量先类型校验 → 结构/字段/片段/循环/别名冲突校验
  cost/estimator.ts       静态成本估计，输出逐字段成本树（声明上界）
  kernel/
    ledger.ts             运行时预算账本：逐笔扣减，超限抛 BudgetExceeded
    executor.ts           执行内核：真实基数、逐元素扣减、取消传播、部分结果
  state/
    adapter.ts            状态适配接口（内核不依赖 SQLite）
    sqliteAdapter.ts      node:sqlite 实现，惰性逐行迭代列表
  fixtures/               合成原始数据与 schema 声明
  engine/engine.ts        编排：解析→变量校验→估计→静态门→执行；统一错误归类
  diag/                   运行编号、JSONL 运行日志、成本树渲染、场景、重放 CLI
  http/routes.ts          Fastify 诊断接口
  app.ts / server.ts      装配工厂 / 服务入口
test/                     单元 + 集成测试（含独立参考答案 test/helpers/reference.ts）
```

### 数据与错误契约

- `CostNode`（`src/contract/types.ts`）是估计树和实际树共用的同构结构，
  可逐节点对比；列表节点带 `cardinality: { declared, actual? }`，
  被取消的未完成节点带 `incomplete: true`。
- 执行器输出的成本树在输出时按结构汇总，**根 `total` 恒等于已扣减成本**（有测试锁定）。

---

## 4. 错误语义（四类必须可区分）

| 类别 | 含义 | 触发阶段 | HTTP |
| --- | --- | --- | --- |
| `INPUT_INVALID` | 适用的输入错误：语法错误、未知字段/片段、变量类型不符、缺选择集、请求体非法 | `REQUEST/PARSE/VALIDATE` | 400 |
| `STATE_CONFLICT` | 状态冲突：同一别名绑定不同字段、变量重复定义、参数重复、**片段循环展开** | `VALIDATE` | 409 |
| `RESOURCE_EXHAUSTED` | 资源耗尽：**静态**估计超预算；运行时超限不报错而是 `PARTIAL` | `STATIC_GATE` | 507 |
| `COMPUTATION_FAILED` | 计算失败：适配器缺解析器、缺表缺列、SQLite 底层错误 | `EXECUTE` | 500 |

运行时预算超限**不是错误响应**：HTTP 仍为 `200`，响应体
`result.status === "PARTIAL"`，并带 `result.cancelled`：

```jsonc
{
  "result": {
    "status": "PARTIAL",
    "estimatedCost": 921,
    "actualCost": 1200,
    "budget": 1200,
    "estimateBelowActual": true,
    "cancelled": {
      "path": "$.users.8.posts.5.tags.3.weight",
      "spent": 1200,
      "budget": 1200,
      "reason": "runtime budget exceeded ... 1200 + 3 > budget 1200"
    },
    "costTree": { /* 逐字段树；未完成元素标 incomplete */ },
    "data": { "users": [ /* 已完成前缀 */ ] }
  }
}
```

变量**先**完成类型校验：`$uid: INT!` 收到字符串时，在任何字段结构校验之前即拒绝。

---

## 5. 快速开始

要求 Node.js ≥ 22.12（用到内置 `node:sqlite`）。

```bash
npm install

# 类型检查
npm run build

# 运行测试（36+ 个断言具体数值/失败类别的用例）
npm test

# 规范场景重放：短而深列表 / 重复别名 / 循环片段 / 估计低于实际 / 静态门 / 变量类型 / 别名冲突
npm run replay                 # 全部
npx tsx src/diag/replay.ts S4-estimate-below-actual   # 单个

# 一键本地演示：重放全部场景 + 起 HTTP 服务打真实请求
npm run demo

# 启动服务（默认 0.0.0.0:3000，可用 PORT 覆盖）
npm start
```

### HTTP 用法

```bash
curl -s http://127.0.0.1:3000/health

curl -s -X POST http://127.0.0.1:3000/query \
  -H 'content-type: application/json' \
  -d '{
        "runId": "manual-1",
        "budget": 1200,
        "query": "{ users { id posts { id tags { name weight } } } }"
      }'

curl -s http://127.0.0.1:3000/runs                 # 运行编号 + 判定汇总
curl -s http://127.0.0.1:3000/runs/manual-1        # 某次运行的完整阶段记录（可重放）
```

### 从日志离线重放

每次运行以 JSONL 追加到 `RUN_LOG`（默认 `logs/runs.jsonl`），
记录运行编号、请求、各阶段中间状态（PARSE/VALIDATE/ESTIMATE/STATIC_GATE/EXECUTE）、
最终结果与判定理由：

```bash
npx tsx src/diag/replay.ts --from-log logs/runs.jsonl
```

---

## 6. 复现：预算扣减与取消传播

内置 7 个固定 `runId` 的规范场景（`src/diag/scenarios.ts`）：

| runId | 验证点 | 预期 |
| --- | --- | --- |
| `S1-deep-list` | 短而深的列表 users→posts→tags | COMPLETE；实际 1765（独立答案一致） |
| `S2-duplicate-alias` | 同键 `role` 选两次 | PARTIAL；第 12 用户第 2 个 `role#dup2` 处取消，重复未绕过预算 |
| `S3-cyclic-fragment` | `...LoopA→...LoopB→...LoopA` | `STATE_CONFLICT`（VALIDATE），不执行 |
| `S4-estimate-below-actual` | 声明 10/5/4，真实 12/6/5 | 估计 921 放行，预算 1200 运行时 PARTIAL，深层取消并保留前缀 |
| `S5-static-gate` | 预算 100 < 估计 181 | `RESOURCE_EXHAUSTED`（STATIC_GATE），执行前拒绝 |
| `S6-typed-variable-bad` | `INT!` 变量传字符串 | `INPUT_INVALID`，变量先校验 |
| `S7-alias-conflict` | 同别名绑不同字段 | `STATE_CONFLICT` |

运行：

```bash
npm run replay          # 每个场景打印阶段状态、判定理由、逐字段【实际】成本树
```

成本树片段（取消沿元素逐层向上标注 `<<INCOMPLETE>>`）：

```
$  self=0 children=1200 total=1200
└─ users  self=1 children=1199 total=1200 [card declared=10 actual=8]
  ├─ [element 0] ... total=134
  └─ [element 8]  total=127  <<INCOMPLETE (cancelled)>>
      └─ [element 5]  total=15  <<INCOMPLETE (cancelled)>>
          └─ [element 3]  total=1  <<INCOMPLETE (cancelled)>>
```

---

## 7. 测试如何保证“不是只测接口能调用”

- 测试断言**具体字面量结果**：估计 1031/921/181/121、实际 1765/1609/145/109、
  取消路径、已完成元素数、首行数据逐字段深相等。
- **参考答案独立于被测核心**：`test/helpers/reference.ts` 直接读
  `src/fixtures/data.ts` 的原始数组长度、用本文件内独立硬编码的倍率计算；
  不经过 parser/validator/estimator/executor。
- 失败类别逐类断言（400/409/507/500 与 `PARTIAL`），而非“不抛异常即可”。
- 覆盖率：语句 ≈ 96%、行 ≈ 95%（`node --experimental-test-coverage`）。

```bash
npm test
# 带覆盖率
node --import tsx --experimental-test-coverage --test test/*.test.ts
```

---

## 8. 设计说明与边界

- 内核通过 `StateAdapter` 接口与存储解耦；SQLite 只是一个实现，
  列表以惰性 `Iterable` 暴露，使执行器能在任意元素后停止拉取（取消传播）。
- 同响应键选择同字段多次（`id id` 或重复别名）在数据上合并，
  但成本树保留每次出现（路径后缀 `#dup2…`），确保重复计费、可审计。
- 片段循环只在**查询实际展开到**该循环时才是冲突；schema 允许声明互相引用的片段。
- 这是教学/验证规模的系统：查询语言为 GraphQL 风格子集（无联合/接口/指令），
  成本为整数单位，预算为非负整数。
