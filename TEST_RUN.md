# 测试运行记录

最近一次真实执行：2026-09-27（环境 Node v22.23.3 / npm 10.9.9 / Linux）。

## 1. 类型检查与构建

```
$ npm run typecheck      # tsc -p tsconfig.test.json --noEmit
（无输出，退出码 0）

$ npm run build          # tsc -p tsconfig.json -> dist/
（无错误，退出码 0）
```

## 2. 单元 / 集成 / golden 测试

```
$ npm test               # vitest run

 ✓ tests/unit/parser.test.ts (11)
 ✓ tests/unit/execute-bubbling.test.ts (10)
 ✓ tests/unit/execute-order.test.ts (3)
 ✓ tests/unit/coercion.test.ts (7)
 ✓ tests/unit/validation.test.ts (19)
 ✓ tests/unit/redact.test.ts (3)
 ✓ tests/unit/run-pipeline.test.ts (5)
 ✓ tests/golden.spec-cases.test.ts (21)
 ✓ tests/integration/http.test.ts (8)

 Test Files  9 passed (9)
      Tests  87 passed (87)
```

**失败项：0。** 开发过程中曾出现并已修复的真实失败（保留以说明核验过程）：

1. 根选择集结果对象未被 `executeOperation` 采用（返回空 `{}`）——修复为返回 `executeSelectionSet` 的结果。
2. 可空字段 `[T!]` 的元素非空失败曾错误上抛——修复为在字段类型边界（字段本身可空时）落 null。
3. SQLite 播种把 JSON 数组/布尔直接绑定导致 `can only bind ...`——改为显式序列化与 0/1。
4. 列表排序：字符串 id 字典序使 `p-explode` 排到 `p1` 前，与手写答案不符——夹具 id 改为自然排序的 `p4`。
5. 操作名未知被误标为 `indeterminate`——修正为：仅多操作歧义是 indeterminate，无操作/未知名为 rejected。

## 3. 覆盖率

```
$ npm run coverage
All files | % Stmts 83.77 | % Branch 80.83 | % Funcs 91.83 | % Lines 83.77
```

达到 80% 下限（语句/分支/函数/行）。`src/graphql/ast.ts`（纯类型，编译后无运行时代码）与 `src/main.ts`（启动入口）已在覆盖率配置中排除并注明原因。

## 4. HTTP 冒烟（真实启动 dist/ 服务）

`node dist/main.js`（端口 4123，临时文件库）实测：

| 请求 | 实测结果 |
|------|----------|
| `{ strictTags }` | HTTP 200，`data:null`，error.path=`["strictTags",2]`，category=RESOLVER |
| `{ looseTags }` | HTTP 200，`data:{looseTags:null}`，path=`["looseTags",1]` |
| `{ user(id:"u1"){ id id: name } }` | HTTP 200，无 data，category=VALIDATION（same response key） |
| 片段 A→B→A | 无 data，VALIDATION，消息含 `A -> B -> A` |
| `$n:Int!` 传 `"abc"` | 无 data，COERCION |
| `mutation { a:bump b:bump }` | `a.value=1, b.value=2`（串行） |
| `/diagnostics?requestId=...` | 命中记录；`password` 显示 `<redacted>`；输出无 `hunter2`、无邮箱明文 |

冒烟后已停止临时服务并删除 `data/`（该目录在 `.gitignore`）。

## 5. 明确未执行 / 不在范围内的项

- **未做**：interface / union 的类型收窄（本系统无抽象类型，片段按类型名相等判定）。
- **未做**：指令执行（`@skip/@Include` 可解析但不生效）；`__schema`/`__type` 内省查询。
- **未做**：subscription（校验阶段明确拒绝）。
- **未做**：GraphQL 批处理/多操作单 HTTP 数组、持久化查询、订阅传输。
- **未做**：鉴权/限流/CSRF（合成本地后端，无真实参与者；HTTP 仅监听 127.0.0.1）。
- **未做**：N+1 批处理（DataLoader）——resolver 直接使用 better-sqlite3。
- **未做**：前端 UI；仅 HTTP + JSON。
- **未执行**：跨平台（Windows/macOS）与长时间持久库的压测；仅在 Linux + Node 22 上实测。
