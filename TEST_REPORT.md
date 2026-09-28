# 测试报告

测试框架：Node.js 内置 `node:test`（经 `tsx` 直接运行 TypeScript），无额外测试运行时依赖。
数据库为真实 `node:sqlite` 的内存库 + 生产迁移 + 确定性合成夹具。

## 1. 最近一次完整运行

命令：

```bash
npm test
```

结果（在本机 Node v22.23.3 实际执行）：

```
# tests 78
# pass 78
# fail 0
# skipped 0
# duration_ms ≈ 1255
```

分层统计（分别运行计数）：

| 层 | 文件 | 用例数 | 结果 |
| --- | --- | --- | --- |
| 单元 | `tests/unit/parser.test.ts` | 6 | 通过 |
| 单元 | `tests/unit/executor.test.ts` | 23 | 通过 |
| 单元 | `tests/unit/coercion.test.ts` | 9 | 通过 |
| 单元 | `tests/unit/redact.test.ts` | 4 | 通过 |
| 集成 | `tests/integration/execution.integration.test.ts` | 22 | 通过 |
| HTTP | `tests/http/http.test.ts` | 14 | 通过 |
| 合计 |  | **78**（单元 42 + 集成 22 + HTTP 14） | **全绿** |

各文件单独运行与 `npm test` 一次性运行均为 78 项全过。

类型检查：`npm run typecheck`（`strict` + `noUncheckedIndexedAccess`，覆盖 `src` 与 `tests`）通过；
生产构建 `npm run build` 通过并冒烟导入 `dist/`。

另做了**编译产物 + 真实 HTTP** 端到端冒烟（非测试框架内）：
启动 `dist/server.js`，用 `curl` 逐项验证 `p-fp-element` / `p-fp-listnull` / `c-fp-nullbody` /
`p-fp-throw`、片段循环、别名冲突、变量类型错误、mutation 顺序夹具与诊断接口，输出与预期一致。

## 2. 测试如何保证不是“接口能调用即可”

- 断言**具体数据**：固定夹具 ID（`u-01`、`p-01`、`p-fp-*`、`c-fp-*`）、字段值、列表顺序、计数
- 断言**错误路径**：如 `post.related.1`、`post.comments.1.body`、`post.archivedComments`
- 断言**失败类别**：`extensions.category`（`NON_NULL_VIOLATION` / `RESOLVER_FAILURE` /
  `COERCION_FAILURE` / `FIELD_CONFLICT` / `FRAGMENT_CYCLE` / `VARIABLE_TYPE` …）
- 断言**HTTP 状态**：执行前拒绝为 400/404，执行期部分数据为 200，明确区分而非统一 500
- 断言**冒泡位置差异**：可空列表停在列表、非空列表冒泡到根、可空元素只置该元素为 null
- 断言**调度语义**：query 并发（总耗时 < max+余量）、mutation 顶层按文档序（自增序号）
- **参考答案独立**：内核单元测试使用测试自建的 `Box/Item` 最小 schema（经通用工厂 `makeSchema`），
  期望值手写，不经生产 schema 的解析器产生，避免“答案由被测核心自身生成”

## 3. 开发过程中实际出现并修复的失败（保留记录）

以下均为 TDD 过程中测试先红、随后定位为**真实缺陷或夹具/期望错误**并修复的项：

1. **`@include(if:false)` 布尔取反写反**（真实内核 bug）
   `collect.shouldIncludeNode` 对 include 错误地做了 `include = !value`，导致
   `@include(if:false)` 的字段仍被包含。修复为 `include = value`。
2. **语法错误被当成 500**（真实缺陷）
   准备阶段未把 `ParseError` / `LexerError` 包装为 `SYNTAX` 类别的 `GraphQLError`，
   HTTP 层因此落入 500。修复后统一映射为 400 `SYNTAX`，诊断阶段记为 `parse`。
3. **标量输出强制错误缺少 path**（真实内核缺陷）
   `completeLeaf` 对 `badCount: Int` 返回字符串的 `COERCION_FAILURE` 未在原点附加 `path/locations`，
   导致 `errors[0].path` 为 `undefined`。修复为在字段原点补齐路径与位置。
4. **变量强制依赖 schema 查找内置标量**（设计缺陷）
   `coerceVariable` 对 `String/ID/...` 先走 `assertScalarOrEnum` 查找，独立单元测试（无 schema）
   抛 `Unknown type`。重构为内置标量直接强制，自定义标量/枚举才走查找。
5. **非空列表元素的 null 丢失索引路径**
   列表递归时剥掉了元素非空性且未累积路径。改为带 `basePath` 的递归，错误携带具体索引（如 `[1]`）。
6. **依赖版本不存在导致安装失败**
   初版声明 `fastify@^4.29.4`（4.x 最新仅到 4.29.1），`npm install` 报 ETARGET。
   已查询 registry 修正为 `^4.29.1`，并生成提交了 `package-lock.json`。
7. **脱敏正则漏判 camelCase / 无分隔符变体**
   旧正则对 `userEmail`、`emailAddress`、`x-token`、`apiKey` 等漏判。
   重写为“归一化精确匹配 + camelCase 分词 + 子串包含”三段判定，并避免裸 `auth` 误伤 `author`。
8. **测试自身的错误期望**（非产品缺陷）
   - 最初误把 `name: id` 当作别名冲突（实为合法别名），改为 `x: id x: name` 才是真冲突；
   - 未终止字符串测试错误地断言具体列号，改为断言错误类型与“Unterminated”语义；
   - 未闭合选择集期望措辞与实现不符，改为断言 `Unterminated selection set`；
   - 共享库自增序号的跨测试顺序耦合，改为断言 `last == first + 1` 的相对关系。

## 4. 未执行 / 未覆盖项（明确保留）

- **未做覆盖率百分比统计**：本项目使用 Node 内置 runner，未接入 c8/istanbul 生成百分比报告；
  全局规则的 80% 门槛未以工具数字形式核验。覆盖面以关键路径枚举为准（见下），如需数字可
  `node --experimental-test-coverage --test ...` 补跑。
- **未做压力 / 性能基准**：仅用 60–120ms 的并发夹具断言调度语义，未做吞吐、内存、长连接压测。
- **未做跨平台验证**：仅在 Linux x64 / Node v22.23.3 运行；未在 macOS/Windows 实测
  （`node:sqlite` 在 22.5+ 提供，理论可用但未验证）。
- **未启用鉴权 / TLS**：本地服务默认绑定 `127.0.0.1`，诊断接口无鉴权（设计为本地运维用途）；
  未实现用户认证、CSRF、速率限制等生产网关能力——这超出“本地合成夹具后端”的范围。
- **未覆盖被刻意排除的特性**：接口/联合、subscription、自定义指令、introspection、
  Object 输入值、Float 均在受限范围外，因此没有对应正向用例，也不应被支持。
- **`LOG_FILE` 落盘路径**仅有代码与类型覆盖，未在测试中断言文件内容（落盘为可选项）。
- **网络外呼**：零外部网络依赖，未也无法对真实第三方服务做集成测试。

## 5. 验收重点对应的测试位置

| 验收点 | 单元测试 | 集成/HTTP 测试 |
| --- | --- | --- |
| 嵌套非空列表冒泡位置 | `executor.test.ts`：嵌套列表 `[i][j].field`、可空/非空列表对比、列表本身为 null | `execution.integration.test.ts`：`p-fp-element` / `p-fp-listnull` / `c-fp-nullbody` |
| 别名冲突 | `executor.test.ts`：不同字段名/不同实参/叶与对象 | HTTP 400 `FIELD_CONFLICT` |
| 片段循环 | 直接环 + 间接环（含环路径消息） | HTTP 400 `FRAGMENT_CYCLE`、`FRAGMENT_NOT_FOUND` |
| 解析器失败 | 可空字段停本地、非空冒泡根 | `p-fp-throw` 的 `title!` / `faultyNote`、`boom` mutation |
| query 并发 / mutation 按序 | `slow` 并发计时、`pulse` 顺序 | `echoDelay` 并发、`recordPulse` 顺序、失败不阻断后续 |
| 变量类型执行前校验 | 必填缺失、布尔传 Int、可空传非空、列表 null 元素 | HTTP 400 `VARIABLE_TYPE` |
| 错误路径 + 部分数据 | 多个冒泡矩阵用例 | 每个夹具都断言 `data` 的部分形态 |
| 诊断标识与脱敏 | `redact.test.ts` | `http.test.ts`：按 `x-request-id` 关联、三态 decision、无敏感泄漏 |
