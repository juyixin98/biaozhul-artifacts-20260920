# multipart/form-data 接收后端

从零搭建的 **TypeScript + Node.js + Fastify + 内置 SQLite** 服务，流式接收
`multipart/form-data`，支持普通字段部件与**受限文件部件**。解析器是自研的
跨网络块（chunk）字节状态机：分隔符可能落在任意两个输入块之间，正文内的
“近似边界”不会被误切；表单仅在**终止边界验证通过后**才对查询可见。

---

## 1. 工程边界（模块与契约）

```
src/
  protocol/                         契约解析 + 执行内核（不依赖 Fastify/SQLite）
    errors.ts                       四类错误法与错误码、HTTP 状态映射、I/O 包装
    types.ts                        Limits / FilePolicy / PartInfo 数据契约
    header-values.ts                媒体类型、参数、Content-Disposition、RFC5987
    part-headers.ts                 部件头块解析（重复头部、严格行语法）
    multipart-parser.ts            执行内核：跨块边界状态机
  storage/                          状态适配
    policy.ts                       受限文件策略（扩展名/MIME/文件名安全）
    upload-session.ts               请求级临时目录、字段缓冲、文件落盘、提交门禁
    submission-store.ts             SQLite 原子事务（提交后才可见）
  diagnostics/
    run-logger.ts                   JSONL 运行日志（运行编号/中间状态/判定理由）
  app/
    app.ts                          Fastify 路由与诊断接口
    upload-service.ts               请求流 → 解析器 → 提交门禁/取消清理
  config.ts                         仅从本地环境变量读取配置
  server.ts                         进程入口
tests/                              1485 个测试（见第 6 节）
scripts/build_golden.py             独立黄金字节向量生成器（Python 标准库）
fixtures/golden/                    已提交的 .bin 与 golden-manifest.json
fixtures/sample/                    最小数据夹具（1x1 PNG、notes.txt）
examples/                           curl 与 Node fetch 调用示例
```

模块间错误契约统一为 `MultipartError { code, errorClass, httpStatus, details, offset }`，
数据契约为 `PartInfo` / `ParseResult` / `StoredPartRow`（见各模块顶部注释）。

### 四类可区分的失败

| errorClass       | 含义                | HTTP | 典型 code |
|------------------|---------------------|------|-----------|
| `INPUT_ERROR`    | 客户端报文/策略不合法 | 400  | `MALFORMED_HEADER_SYNTAX`、`PATH_TRAVERSAL_FILENAME`、`MISSING_TERMINATING_BOUNDARY` |
| `STATE_CONFLICT` | 生命周期冲突          | 409  | `PARSER_FINISHED`、`PARSER_ABORTED`、`COMMIT_BEFORE_COMPLETION`、`UPLOAD_CANCELED` |
| `RESOURCE_LIMIT` | 配额耗尽             | 413  | `PART_SIZE_EXCEEDED`、`TOTAL_SIZE_EXCEEDED`、`HEADER_SIZE_EXCEEDED`、`MAX_*_EXCEEDED`、`DISK_SPACE` |
| `COMPUTE_FAILURE`| 非输入导致的 I/O/DB   | 500  | `IO_FAILURE`、`SQLITE_FAILURE` |

错误体示例：

```json
{ "error": "PATH_TRAVERSAL_FILENAME", "errorClass": "INPUT_ERROR",
  "message": "filename: filename must not contain path separators",
  "httpStatus": 400, "details": { "filename": "../../evil.sh" } }
```

---

## 2. 明确的解析规则

- **边界切分**：仅在 `CRLF "--" boundary [LWSP] (CRLF | "--" CRLF)` 处切分；
  候选后缀校验失败则按普通正文处理，并从候选的 `CRLF` 之后继续扫描。块末
  落在“可能的分隔符”内时，保留该重叠后缀等待后续字节（或在 `end()` 判定截断）。
  RFC 2046 transport-padding（SP/HTAB）容忍但上限 100 字节。
- **头部**：字段名须为 RFC 9110 token；值仅允许 VCHAR/SP/HTAB；拒绝 obs-fold、
  obs-text；头块必须为合法 UTF-8。同一部件内 **重复 Content-Disposition /
  Content-Type 即致命错误**。
- **参数**：严格解析，支持 quoted-string 与 quoted-pair；**同一头部内重复参数**
  为 `MALFORMED_HEADER_SYNTAX`。
- **文件名编码**：`filename`（quoted）按字面值；`filename*` 走 RFC 5987
  `charset'lang'value`，**仅接受 UTF-8**，百分号解码非法即失败；两者并存时
  `filename*` 优先。
- **文件名安全**：拒绝空名、控制字符/NUL、`/`、`\`、任何 `..`、首尾空白、尾点。
- **受限文件**：扩展名白名单 + MIME 白名单（文件部件缺 Content-Type 直接拒绝）+
  文件名长度上限。字段部件默认按 RFC 7578 作 UTF-8 文本，非 UTF-8 拒绝。
- **限额（相互独立）**：单字段、单文件、总正文、单头块、部件/字段/文件数量。
- **提交门禁**：解析全程写入**请求级临时目录**；仅在 `parser.end()` 验证终止
  边界后，才把临时文件 rename 到永久目录并在**一个 SQLite IMMEDIATE 事务**内
  发布。任一步失败 → 回滚事务、删除已提升文件、只清理本请求临时目录。
- **取消**：客户端断连/abort → `UPLOAD_CANCELED`（STATE_CONFLICT），不落库、
  不留临时文件。

---

## 3. 本地运行

要求 Node.js ≥ 22.5（使用内置 `node:sqlite`，启动需 `--experimental-sqlite`）。

```bash
npm install        # 依赖已锁定到 package-lock.json
npm run dev        # tsx 直接跑 TS，监听 127.0.0.1:3000
# 或
npm run build && npm start
```

常用环境变量（默认值见 `src/config.ts`）：
`PORT` `HOST` `DATA_DIR` `TMP_DIR` `FILES_DIR` `DB_PATH` `RUN_LOG_PATH`
`LIMIT_FIELD` `LIMIT_FILE` `LIMIT_TOTAL` `LIMIT_HEADER`
`LIMIT_MAX_PARTS` `LIMIT_MAX_FIELDS` `LIMIT_MAX_FILES` `REQUIRE_FILE_PART`。

### 接口

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/upload` | 流式接收 multipart（非该媒体类型返回 415） |
| GET  | `/healthz` | 存活与计数 |
| GET  | `/diagnostics/limits` | 生效限额与文件策略 |
| GET  | `/diagnostics/runs?tail=N` | JSONL 运行日志尾部（可重放） |
| GET  | `/submissions?limit=N` | 已提交记录列表 |
| GET  | `/submissions/:id` | 单条已提交记录（未提交不可见 → 404） |
| GET  | `/submissions/:id/file/:partIndex` | 下载已提交文件（带 SHA-256 头） |

### 调用示例

```bash
examples/curl-examples.sh           # curl：正常 + 各类异常
BASE=http://127.0.0.1:3000 node examples/node-client.mjs
bash scripts/smoke.sh               # 启动编译产物并断言状态码/资源回收
```

---

## 4. 最小数据夹具

- `fixtures/sample/pixel.png`：75 字节 1×1 PNG（合成，无外部依赖）。
- `fixtures/sample/notes.txt`：纯文本。
- `fixtures/golden/`：6 个正向量、5 个负向量 `.bin` 与 `golden-manifest.json`。

黄金向量由 **Python 标准库独立生成**（`scripts/build_golden.py`，hashlib 摘要、
手工拼字节），**不经过被测 TS 代码**；`.bin` 与清单已提交，跑测试无需 Python。
需要重新生成：`python3 scripts/build_golden.py fixtures/golden`。

---

## 5. 运行日志（可重放）

`data/run-log.jsonl` 每行一个 JSON：`runId`、`phase`（open/progress/verdict）、
关键中间状态（解析状态、计数）、终判 `verdict` 与 `reasonCode/reason`、状态机
事件、耗时。一次失败仅凭该日志即可重放：

```bash
curl -s 'http://127.0.0.1:3000/diagnostics/runs?tail=50' | jq .
```

---

## 6. 测试与复现结果

```bash
npm test          # = vitest run（内部以 node:sqlite 运行）
npx tsc -p tsconfig.json --noEmit   # 严格类型检查
```

1485 个测试全部通过（实测于 Node v22.23.3）：

- `golden-vectors.test.ts`（1407）：每个正向量在**每一个两刀切点**（0..wireLength）、
  固定块长 1..8/33、5 个随机种子、单字节下重放，逐部件断言
  名称/文件名/Content-Type/大小/**SHA-256**/字段值与各类计数；负向量断言
  **具体错误码与错误类别**（含 1 字节分块）；专项断言 v3 中 7 个非法分隔候选
  被识别且 199 字节正文原样重组。
- `header-contract.test.ts`（41）：引号参数、转义、重复参数/头部、缺结束引号、
  RFC 5987 各非法情形、路径穿越文件名等。
- `limits-state.test.ts`（17）：单部件/总量/头长/数量限额、完成后写入、abort、
  截断分类、ENOSPC→DISK_SPACE 等。
- `storage-session.test.ts`（8）：提交门禁、双重提交、NO_FILE_PARTS、重名、
  失败/取消后**只清理本请求临时数据**、落盘字节与 SHA 往返。
- `http-api.test.ts`（10）：经 Fastify 完整链路的正常/异常、碎块流、下载比对、
  诊断日志四类判定。
- `http-cancel.test.ts`（2）：**真实 TCP** 正常上传，以及 `AbortController`
  中途断连后临时目录/永久目录/SQLite 全部回收、日志记录 `CANCELED/UPLOAD_CANCELED`。

真实服务（编译产物）已人工核验：201 正常提交并可查询/下载（SHA 一致）、415、
400（穿越名/缺终止边界）、413（超限）、断连取消后 `tmp=0 files 不变 submissions 不变`。
