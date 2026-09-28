# OpenAPI 3.1 子集 · 接口契约差异服务

判定两个版本的 OpenAPI 3.1 契约是否**破坏性兼容**，并从**客户端请求**与
**服务端响应**两个方向分别给出结论；每个破坏性结论都附带一个**最小、可独立
验证的不兼容请求/响应见证（witness）**。所有数据均为本地合成夹具，无外部账号。

## 1. 支持的 OpenAPI 3.1 子集与算法假设

**结构层面**
- 操作按 `METHOD + 路径模板` 匹配（如 `GET /pets/{id}`）；仅存在于旧版的操作
  → 破坏性（每个旧请求都失败）；仅存在于新版 → 兼容（增量）。
- 参数按 `(name, in)` 二元组匹配，而不是只看字段增删：同名参数从
  `header` 移到 `query` 判定为 **PARAM_LOCATION_CHANGED**（旧位置被忽略）。
- 必填性变化（参数、requestBody、对象属性）单独分类。
- 响应状态码矩阵独立处理：
  - 新增状态码且旧契约无该码、也无 `default` → **STATUS_CODE_SPLIT**（破坏，
    旧客户端没有对应分支）；
  - 状态码从契约消失 → **STATUS_CODE_REMOVED**，判为 **undetermined**
    （文档不承诺 ≠ 服务器不再产生）。

**Schema 层面（双向极性相反）**
- 请求方向：“旧契约接受、新契约拒绝”为破坏（枚举收窄、可空变不可空、
  新增必填属性、`additionalProperties` 收紧、类型收窄、const 改变）。
- 响应方向：“新版会产生、旧客户端不接受”为破坏（枚举**扩展**、新增可空、
  必填响应属性变可选、新增属性撞上旧客户端 `additionalProperties:false`）。
- 因此同一变化在两个方向结论不同，例如请求参数枚举扩展（`cat,dog`→
  `cat,dog,bird`）兼容，而响应枚举扩展破坏；请求侧可空移除破坏，服务端输出
  去掉 `null` 则是安全收窄。
- `default` 变化是 **informational**（省略时解析结果不同，不是线上拒绝）。
- `x-*` 扩展**永不判破坏**，单列在 `extensionNotes`。
- 子集外的 JSON Schema 关键字（`pattern`、`minimum`、`oneOf`、`format` 等）
  → **UNKNOWN_KEYWORD**，结论降级为 **undetermined**，不猜判。

**引用与有界性**
- 仅支持文档内 `$ref`（`#/components/...`）；缺失 → UNRESOLVED_REF，
  外部 URL → 标记不支持，均为 undetermined。
- 递归 `$ref`（如自引用树节点）通过解析栈 + 深度上限（schema 16 / diff 12）
  + 同分支同子树去重实现**有界解析**；两侧完全相同的递归子树被短路，
  不会误报 CYCLIC_REF。

**见证（witness）构造**
- 每个 schema 级破坏见证 = “**接受方根模式的最小合法实例**”上，仅替换/省略
  肇事叶子。例如枚举收窄见证为 `{name:"x",kind:"bird"}`（旧接受、新拒绝），
  必填属性见证为恰好缺少该属性的对象。
- 因此见证可以直接喂给独立验证器（测试中用 **Ajv** 第三方实现，而非被测内核）
  双向核验。

> 已知边界：请求参数被删除、响应属性被删除且 `additionalProperties` 非 false
> 等情形，OpenAPI 不规定对端是拒绝还是忽略，统一输出 **undetermined** 并给出
> 原因，而不是伪造一个“必然拒绝”的见证。

## 2. 模块关系

```
src/
  contract/      契约解析（独立模块）
    loader.ts       YAML/JSON + 3.1 结构校验（错误带位置）
    ref-resolver.ts 本地 $ref 指针解析、循环/缺失/外部边界
    normalize.ts    归一化模型：操作/参数(按 name+in)/请求体/响应
    types.ts        模型类型、已知/注解关键字集合
  kernel/        执行内核（无 I/O、无框架依赖）
    value-space.ts  值空间采样与 injectLeaf 最小见证构造（有界）
    schema-diff.ts  schema 递归双向比较 + 见证/不确定结论
    diff.ts         操作/参数/请求体/状态码矩阵编排
    types.ts        FailureCategory 固定分类法与 Finding/Witness 类型
  state/         状态适配
    store.ts        SQLite(better-sqlite3)：契约版本、分析、发现、处理日志
  diagnostics/   诊断接口
    service.ts      parse→normalize→diff 编排，分步日志，失败/不确定分列
    http.ts         Fastify：POST /v1/diff、分析与日志查询、X-Request-Id 关联
  config/        环境配置（PORT、CONTRACT_DIFF_DB，带本地默认值）
  main.ts        HTTP 入口；cli.ts 本地文件 CLI
tests/           独立测试（夹具 + 单元 + Ajv 见证 oracle + HTTP 集成）
examples/        pets-v1.yaml / pets-v2.yaml 合成契约矩阵
```

依赖方向始终是 `diagnostics → state/kernel → contract`，内核不反向依赖。

## 3. 依赖版本

| 依赖 | 版本 | 用途 |
|---|---|---|
| Node.js | ≥ 20（开发验证于 22.23.3） | 运行时 |
| fastify | ^5.2.0 | HTTP 诊断接口 |
| better-sqlite3 | ^11.10.0 | SQLite 状态/日志 |
| yaml | ^2.7.0 | 契约 YAML/JSON 解析 |
| typescript | ^5.7.2 | 严格类型（`strict` + `noUncheckedIndexedAccess`） |
| vitest | ^2.1.x | 测试与 v8 覆盖率 |
| ajv | ^8.17.1（仅测试） | **独立第三方** JSON Schema 见证核验 |
| tsx | ^4.19.2 | 本地直接运行 TS |

## 4. 本地验证命令

```bash
npm install

# 1) 类型检查
npm run typecheck

# 2) 全部测试（72 个）+ 覆盖率门槛（语句80/分支75/函数85）
npm test                 # 或: npx vitest run --coverage

# 3) CLI：对两个契约文件做差异分析，打印 JSON（含见证与分步日志）
npx tsx scripts/extract-examples.ts        # 由夹具生成 examples/*.yaml（已附带）
npx tsx src/cli.ts examples/pets-v1.yaml examples/pets-v2.yaml

# 4) 启动 HTTP 服务并冒烟
CONTRACT_DIFF_DB=:memory: PORT=3477 npm start
curl -s http://127.0.0.1:3477/healthz
jq -nc --rawfile old examples/pets-v1.yaml --rawfile new examples/pets-v2.yaml \
  '{oldContract:$old,newContract:$new}' > /tmp/req.json
curl -s -X POST http://127.0.0.1:3477/v1/diff \
  -H 'content-type: application/json' -H 'x-request-id: smoke-42' \
  --data @/tmp/req.json | jq '{status, requestId, failures: [.failures[].category], uncertainties: [.uncertainties[].category]}'
```

### 预期判断方式
- `GET /healthz` → `{"status":"ok"}`。
- `POST /v1/diff` 返回 `status:"breaking"`，响应头/体含同一 `X-Request-Id`。
- `failures` 应包含（请求方向）`PARAM_LOCATION_CHANGED`（X-Tenant header→query）、
  `ENUM_NARROWED`（kind 删除 bird）、两个 `NULLABILITY_REMOVED`、
  `PROPERTY_MADE_REQUIRED`（kind）、`PARAM_MADE_REQUIRED`（trace/limit）、
  `REQUEST_BODY_MADE_REQUIRED`、`OPERATION_REMOVED`；
  （响应方向）`STATUS_CODE_SPLIT`（新增 500）、`ADDITIONAL_PROPERTIES_TIGHTENED`
  （新增 page）、必填 `tags` 变可选、`id` 新增可空。
- `uncertainties` 单列：`STATUS_CODE_REMOVED`（404 消失）、被删属性 `note`
  （extras 被禁）、被删参数等，每条带 `uncertainty` 原因。
- tag 枚举**扩展**不产生破坏项；`x-internal-hint` 仅出现在 `extensionNotes`。
- 任一破坏性 schema 见证都被测试用 **Ajv** 验证为：请求方向旧接受/新拒绝，
  响应方向新接受/旧拒绝。
- `GET /v1/analyses/:id/logs` 用同一 requestId 串起 receive→parse-old/new→
  diff→verdict→persist 的关键步骤与版本信息。

## 5. HTTP 接口

| 方法/路径 | 说明 |
|---|---|
| `POST /v1/diff` | body `{oldContract,newContract,contractRef?,oldVersionLabel?,newVersionLabel?}`；返回 status/failures/uncertainties/result/steps |
| `GET /v1/analyses/:id` | 取持久化分析与全部发现（含见证） |
| `GET /v1/analyses/:id/logs` | 取关联请求身份的分步处理日志 |
| `GET /healthz` | 健康检查 |

错误契约：请求体缺字段 → 400 `VALIDATION`；契约解析失败 → 422
`CONTRACT_PARSE_ERROR`（带 `position`）；未知分析 id → 404。

## 6. 测试如何避免“自证”

`tests/witness.oracle.test.ts` 不读取内核的 accept/reject 文案，而是把见证值交给
**Ajv 2020-12**（OpenAPI 3.1 的 schema 方言）分别编译旧/新契约组件后独立判定，
并对最小性做断言（如枚举见证恰为 `{kind,name}`）。夹具为手写 YAML，参考结论
由测试显式给出，不由被测实现生成。
