# RFC 6902 受限 JSON 文档更新服务

对存储的 JSON 文档执行受限的 [RFC 6902 JSON Patch](https://www.rfc-editor.org/rfc/rfc6902)，
支持 `test`、`add`、`remove`、`replace`、`move`、`copy` 六种操作。补丁绑定**预期文档版本**，
任一操作失败则整份补丁原子回滚。技术栈：TypeScript + Node.js + Fastify + 内置 SQLite（`node:sqlite`）。

本服务只用本地合成夹具，不依赖任何生产账号或真实业务数据。

---

## 1. 模块划分（各自承担实际工作，无硬编码演示）

| 文件 | 职责 |
|------|------|
| `src/json-pointer.ts` | RFC 6901 指针：按段解析、`~0`/`~1` 转义、数组索引与 `-` 末尾追加语义、祖先前缀判定 |
| `src/contract.ts` | **契约解析层**：补丁结构校验（操作类型、字段、指针语法、move 后代关系），产出类型化失败类别 |
| `src/kernel.ts` | **执行内核**：在深拷贝上顺序执行，操作从前一步结果取值；返回逐步文档快照或类型化失败。纯函数，不碰 IO |
| `src/store.ts` | **状态适配层**：SQLite 持久化、单调版本号、乐观版本绑定；单事务保证内核失败即 `ROLLBACK`，并写审计事件 |
| `src/api.ts` | **诊断/接口层**（Fastify）：补丁端点、文档与事件查询；每条响应/日志关联请求身份 |
| `src/server.ts` | 组装与启动、统一错误映射 |
| `src/logger.ts` | 结构化日志（NDJSON），可注入内存 sink 供测试断言 |
| `src/config.ts` | 环境配置，均有本地默认值 |

数据流：`contract → kernel → store(sqlite tx) → api 响应/日志`。

---

## 2. 快速开始

```bash
node --version        # 需要 Node >= 22.5（使用内置 node:sqlite）
npm ci                # 或 npm install；依赖版本已固定（见 package.json）
npm run build
npm start             # 默认 http://127.0.0.1:3000，数据文件 ./data/patch-service.sqlite
```

开发模式（免构建）：`npm run dev`

一键验证（类型检查 → 构建 → 独立预言机夹具复现 → 368 个测试 → 启动真实服务器做 HTTP 冒烟）：

```bash
npm run verify        # = bash scripts/verify.sh
```

---

## 3. HTTP 接口

所有响应都回显 `requestId`（取请求头 `X-Request-Id`，未提供则用 Fastify 生成 id）。
建议客户端为每次补丁显式设置 `X-Request-Id`，便于在日志与 `/events/:requestId` 中追溯。

### `POST /documents`
```json
{ "id": "doc-1", "document": { "list": [1, 2], "n": 0 } }
```
→ `201`，文档初始 `version = 0`。

### `POST /documents/:id/patch`
```json
{
  "expectedVersion": 0,
  "patch": [
    { "op": "add", "path": "/list/-", "value": 3 },
    { "op": "test", "path": "/list/2", "value": 3 },
    { "op": "move", "from": "/list/0", "path": "/list/2" }
  ]
}
```
成功 `200`：
```json
{
  "requestId": "...",
  "documentId": "doc-1",
  "baseVersion": 0,
  "newVersion": 1,
  "result": { "list": [2, 3, 1], "n": 0 },
  "steps": [ { "index": 0, "op": "add", "path": "/list/-", "status": "applied",
               "resultAfter": { "list": [1, 2, 3], "n": 0 } } /* …每一步后的文档快照 */ ]
}
```

失败（示例：中途 `test` 失败）`422`：
```json
{
  "requestId": "...",
  "success": false,
  "error": {
    "category": "TEST_FAILED",
    "message": "test at /n failed: actual 42 !== expected 0",
    "failedAtIndex": 2,
    "rolledBack": true,
    "appliedBeforeFailure": [ {"index": 0, "op": "add", "path": "/list/-"}, {"index":1,"op":"replace","path":"/n"} ]
  }
}
```

### 诊断端点

- `GET /documents/:id` — 当前文档与版本。
- `GET /documents/:id/events?limit=50` — 该文档补丁事件（`applied` / `rejected` / `conflict`，含版本、失败类别、失败下标、失败前已应用步数）。
- `GET /events/:requestId` — 按请求身份查单个事件。
- `GET /health`。

### 状态码与失败类别

| HTTP | 类别 | 含义 |
|------|------|------|
| 400 | `MALFORMED_JSON` `PATCH_NOT_ARRAY` `OP_NOT_OBJECT` `UNKNOWN_OP` `MISSING_FIELD` `BAD_FIELD_TYPE` `MALFORMED_POINTER` `MOVE_INTO_SELF` `EMPTY_PATCH` | 契约层错误（请求即拒，不接触内核） |
| 422 | `TEST_FAILED` `POINTER_ERROR` `MOVE_TARGET_DESCENDANT` `ROOT_LOCATION_INVALID` `KERNEL_FAILURE` | 内核执行失败，事务已回滚 |
| 409 | `VERSION_CONFLICT` | 补丁绑定版本与当前版本不符 |
| 404 | `DOCUMENT_NOT_FOUND` / `EVENT_NOT_FOUND` | 文档或事件不存在 |
| 409 | `DOCUMENT_ALREADY_EXISTS` | 建文档 id 冲突 |

本受限服务的额外约束：补丁**不能为空数组**；操作对象**不允许出现未知字段**（RFC 允许忽略，这里显式拒绝）。

---

## 4. 边界语义（行为约定如何落实）

1. **JSON Pointer 转义按段解析。** `""` 表示整个文档；`"/"` 是一个空字符串键；`"/a~1b"` 表示键 `a/b`，
   `"/a~0b"` 表示键 `a~b`。解码顺序为先 `~1→/` 再 `~0→~`，故 `a~01b` 解码为字面键 `a~1b`（不是 `a/b`）。
   未转义的 `/` 一定是段分隔符：指针 `/a/b` 绝不会命中键 `"a/b"`。
2. **数组索引与末尾追加。** 索引必须是规范十进制（拒绝 `01`、负数、非数字）；读取/删除/替换必须 `0..length-1`，
   `add` 允许 `0..length` 与专用标记 `-`（追加）。头部/中间 `add` 使右侧元素整体右移，`remove` 使尾部左移。
3. **操作从前一步结果取值。** 每一步针对当前文档惰性解析；`copy`/`move` 读到的是此前操作形成的最新值。
4. **test 失败撤销整份补丁。** 内核在输入的深拷贝上运行；任何一步失败都不产出部分结果，调用方原始文档不变。
   存储侧再包一层 `BEGIN IMMEDIATE` 事务：失败 `ROLLBACK`，磁盘文档与版本号保持原样。
5. **move 不能移到自身后代。** 契约层对 `from === path` 或 `path` 位于 `from` 子树内（前缀按段边界判定，
   不会把 `/ab` 误判为 `/a` 的后代）直接拒绝；内核独立调用时也会自检。
6. **move 的数组位置按"删除后"计算（RFC 6902 §4.4）。** `["a","b","c"]` 上 move `/0 → /1`
   先删除 `a` 得 `["b","c"]`，再插入到位置 1，结果为 `["b","a","c"]`。
7. **补丁绑定预期文档版本。** 每次成功补丁使 `version` 恰好 +1；`expectedVersion` 不等于当前版本即 `409`，
   不执行任何操作。失败（含 test 失败）不推进版本，同一版本可重试。
8. **replace 要求目标已存在；add 根路径会整体替换文档；`remove`/`move from ""` 作用于整个文档被拒绝。**
9. **深比较按 JSON 类型敏感**：`1 !== true`，数组不等于对象，对象按成员集合与递归值比较（结构相等而非引用相等）。

---

## 5. 可解释性（结果与日志）

日志为每行一个 JSON 对象，至少包含：

- `requestId`（请求身份）、`documentId`；
- `phase`：`contract` → `kernel` → `store` → `http`，标明处理位置；
- 版本信息：`expectedVersion` / `baseVersion` / `newVersion` / `actualVersion`；
- `outcome`（success/failure）、`category`（类型化失败类别）、`failedAtIndex`、`stepsAppliedBeforeOutcome`；
- **`certainty`**：可确定的结论标 `certain`；服务无法确证的异常路径标 `uncertain`，绝不把不确定写成成功。

内核成功结果里的 `steps[].resultAfter` 给出**每一步之后的具体文档状态**，失败时 `appliedBeforeFailure`
给出已完成步骤，二者让"原子性与每步文档状态"可直接核验。审计表 `patch_events` 把成功、拒绝、冲突分别落库。

---

## 6. 独立测试与参考实现（答案不来自被测核心自身）

- `test/pointer.test.ts`、`contract.test.ts`、`kernel.test.ts`、`store.test.ts`、`api.test.ts`：
  对**具体结果值、具体失败类别与失败下标**做断言，而非"接口能调用"。
- `test/fixtures/hand-authored-cases.json`：人工策划的边界用例（数组移位、空键、斜线/波浪转义、
  中途失败回滚、move 后代、链式取值等）。
- **独立参考实现**：`scripts/generate-crosscheck.py` 是一份仅用 Python 标准库、与 TypeScript 内核
  完全独立编写的 RFC 6902 实现兼夹具生成器。它对随机补丁给出具体期望结果或
  （具体失败类别 + 失败下标），输出到 `test/fixtures/crosscheck-generated.json`。
  `test/crosscheck.test.ts` 让被测内核逐项与该预言机比对（140 个场景，含 42 个预期失败）。
- **第三方库交叉验证**：同一批场景还与 npm 的 `fast-json-patch` 比对——成功者结果逐字节一致，
  失败者在双方都失败（已知的边界分歧见下，测试中显式建模，不掩盖）。
- `verify.sh` 会重新运行 Python 生成器并 `diff` 已提交夹具，**夹具必须可由独立生成器复现**，
  防止"参考答案由被测实现自己产出"。

覆盖率（`npm test` 配 `--experimental-test-coverage`）：行 97%+ / 分支 89%+ / 函数 96%+。

### 已记录的规范边界分歧（单列，不写成"已通过/已一致"）

- **精确自移动 `move from == path`**：RFC 6902 §4.4 字面只禁止移入"真后代（proper descendant）"，
  对 from 与 path 完全相同的情形未明言。第三方 `fast-json-patch` 将其视为**无变化的成功**且其批量应用
  非原子；本受限服务与独立 Python 预言机按"move 不能移到自身"的约定**拒绝**（契约层 `MOVE_INTO_SELF`）。
  交叉测试对此显式断言：FJP 在此要么拒绝，要么结果恰等于前缀操作后的状态（即自移动本身无副作用），
  绝不允许产生其它改动。真后代移动在三方实现中一致拒绝。

---

## 7. 明确未执行的检查（不声称通过）

- 未做真实网络 TLS / 反向代理终止验证（服务仅在回环地址提供明文 HTTP）。
- 未验证多进程 SQLite 写竞争（仅在单进程单连接 + 事务下验证；多进程部署需另设写串行化或换 WAL 多连接方案）。
- 未做负载/性能测试与认证多租户授权（本地、无账号服务，超出范围）。
- 未使用生产账号、外部服务或真实业务数据。

---

## 8. 配置（环境变量，均有默认值）

| 变量 | 默认 | 说明 |
|------|------|------|
| `HOST` / `PORT` | `127.0.0.1` / `3000` | 监听地址 |
| `DATABASE_PATH` | `./data/patch-service.sqlite` | SQLite 文件；`:memory:` 为内存库 |
| `LOG_LEVEL` | `info` | `debug` 时输出每一步 kernel 日志 |
| `MAX_BODY_BYTES` | `1048576` | 请求体上限 |

依赖版本固定在 `package.json`（fastify、typescript、tsx、@types/node、fast-json-patch 均为精确版本）。
