# 资源版本 API（强/弱 ETag 与条件请求）

基于 **TypeScript + Node.js + Fastify + SQLite** 的资源版本化服务，完整实现
RFC 9110 条件请求语义：强/弱实体标签、`If-Match`、`If-None-Match`、
`If-Modified-Since`、`If-Unmodified-Since`、通配符 `*`，以及乐观并发控制。

所有数据均为本地合成夹具，不依赖任何外部账号或真实业务数据。

---

## 1. 核心语义

### 比较模式按请求方法区分（RFC 9110 §13.2）

| 方法类别 | 条件头 | 比较函数 | 失败结果 |
|---|---|---|---|
| 安全方法 `GET/HEAD` | `If-None-Match` | **弱比较**（§8.8.3.3） | `304 Not Modified` |
| 安全方法 `GET/HEAD` | `If-Modified-Since` | 日期比较 | `304`（INM 不存在时才评估） |
| 状态变更 `PUT/PATCH/DELETE` | `If-Match` | **强比较**（§8.8.3.2） | `412 Precondition Failed` |
| 状态变更 `PUT/PATCH/DELETE` | `If-Unmodified-Since` | 日期比较 | `412` |
| 状态变更 `PUT/PATCH/DELETE` | `If-None-Match` | **弱比较** | `412` |
| 状态变更 | `If-Modified-Since` | — | 忽略 |

- **强比较**：双方都必须是强标签且 opaque 相等；只要任一方带 `W/` 即不匹配。
- **弱比较**：只比较 opaque，忽略双方强弱。
- **通配符 `*`**：`If-Match: *` 要求资源存在；`If-None-Match: *` 要求资源不存在
  （PUT 的“仅创建”语义）。

### 条件优先级（固定，RFC 9110 §13.2.2）

- 安全方法：`If-None-Match` → `If-Modified-Since`（后者在 INM 存在时**被忽略**并记录）。
- 状态变更：`If-Match`（否则 `If-Unmodified-Since`）→ `If-None-Match`。
  第 1 步失败立即返回 412，第 2 步不再评估。

### “不存在”与“版本不匹配”是不同语义

- 资源不存在：`GET/PATCH/DELETE` → **404** `RESOURCE_NOT_FOUND`。
- 对不存在资源发 `If-Match`/`If-Match: *` → **412** `PRECONDITION_IF_MATCH_FAILED`
  （条件为假，而非找不到）。
- 对不存在资源发 `If-None-Match: *` → 通过，`PUT` 创建返回 **201**。

### 条件校验与写入同一事务

每次写操作在**一个 SQLite `BEGIN IMMEDIATE` 事务**内完成：
选择当前版本 → 评估全部前置条件 → 追加新版本 → 回读已提交行。
响应的 `ETag`、`Last-Modified`、`X-Resource-Version` 与响应体**来自同一提交快照**。
两个并发条件写在数据库写锁处串行化，后执行者看到新版本，因此得到 412，
不会发生丢失更新。

### ETag 形式

```
"v<版本号>-<规范JSON的sha256前8位>"      强标签（默认）
W/"v<版本号>-<...>"                      弱标签（GET ?weak=1 显式请求时演示）
```

版本号单调递增；删除追加**墓碑行**（不物理删除），重建时版本号继续递增，
历史不可变，可通过诊断接口回放。

---

## 2. 工程组织

按“契约解析 / 执行内核 / 状态适配 / 诊断接口”分层，传输与存储均可替换：

```
src/
  config.ts                 配置层：环境变量解析，非法配置启动即失败
  contract/                 契约解析层（纯函数，无 IO）
    etag.ts                 实体标签词法、强/弱比较、If 列表解析
    http-date.ts            IMF-fixdate 解析与比较
    conditions.ts           条件评估：方法分派 + 固定优先级 + 判定轨迹
    errors.ts               失败类别（400/304/404/412/500）
  core/                     执行内核（不依赖 Fastify/SQLite）
    kernel.ts               事务内 选择→校验→写入→回读
    merge-patch.ts          JSON Merge Patch (RFC 7386)
    versioning.ts / hash.ts 版本与 ETag 派生
    ports.ts                ResourceStore 端口
  state/
    sqlite-store.ts         SQLite/WAL/BEGIN IMMEDIATE 适配器
    memory-store.ts         独立内存实现（内核测试的测试替身）
  transport/
    http-app.ts             Fastify 路由、错误信封、校验器头
    logger.ts               请求级 runId 的结构化 JSON 日志
  app.ts / server.ts        组装根 / 启动入口
scripts/
  seed.ts                   本地合成夹具（固定时钟，可复现时间戳）
  demo.sh                   真实 curl 端到端演示
tests/
  unit/                     契约/内核/配置 单元测试
  integration/              SQLite、HTTP 夹具重放、真实双客户端并发
  fixtures/http-cases.json  可重放请求/期望夹具
  helpers/oracle.ts         独立预言机（独立实现的规范化+哈希与冻结期望值）
```

测试期望值**不是由被测核心生成**：参考 ETag 由独立实现的规范化器计算，
并冻结为具体字符串（见 `tests/helpers/oracle.ts` 与夹具）。

---

## 3. 从干净目录复现

### 3.1 前置与版本（已锁定）

| 依赖 | 版本 |
|---|---|
| Node.js | ≥ 20.19（验收环境：**v22.23.3**） |
| TypeScript | 5.9.3（devDependency，经 tsx 运行） |
| fastify | 5.12.5 |
| better-sqlite3 | 13.0.3（含原生绑定，npm 自动编译/下载） |
| vitest | 5.0.2 |
| @vitest/coverage-v8 | 5.0.2 |
| tsx | 4.23.15 |

```bash
# 1) 安装（better-sqlite3 为原生模块，需可用的 C++ 工具链或预编译包）
npm install

# 2) 类型检查
npm run typecheck

# 3) 运行全部测试
npm test

# 4) 覆盖率（阈值：语句/分支/函数/行 均 ≥ 80%）
npm run test:coverage
```

### 3.2 配置

| 变量 | 默认值 | 说明 |
|---|---|---|
| `PORT` | `8080` | 监听端口 |
| `HOST` | `127.0.0.1` | 监听地址 |
| `DB_PATH` | `data/app.db` | SQLite 文件（自动建目录/WAL） |
| `LOG_LEVEL` | `info` | 保留字段 |

非法 `PORT`/`LOG_LEVEL` 启动即报错退出。参见 `.env.example`。

### 3.3 启动与种子

```bash
npm run seed          # 写入合成夹具（固定时间戳，已存在数据则跳过）
npm start             # 启动服务
# 另一终端：
npm run demo          # 真实 HTTP + curl 的端到端演示（可用 PORT 覆盖端口）
```

---

## 4. HTTP 接口与请求样例

约定：
- 资源 id 匹配 `[A-Za-z0-9][A-Za-z0-9._-]{0,127}`。
- 响应信封：成功 `{ id, version, body }`；失败 `{ error: { code, message, trace? } }`。
- 每个响应带头 `ETag`、`Last-Modified`、`X-Resource-Version`、`X-Request-Id`。

### 创建（201）与仅创建通配符

```bash
curl -i -X PUT http://127.0.0.1:8080/resources/doc \
  -H 'Content-Type: application/json' \
  --data '{"text":"v0"}'
# HTTP/1.1 201 Created
# ETag: "v1-xxxxxxxx"
# X-Resource-Version: 1

curl -i -X PUT http://127.0.0.1:8080/resources/doc \
  -H 'Content-Type: application/json' -H 'If-None-Match: *' \
  --data '{"text":"v1"}'
# 已存在 → 412 PRECONDITION_IF_NONE_MATCH_FAILED
```

### 条件读：304（弱比较，W/ 也命中）

```bash
curl -i http://127.0.0.1:8080/resources/doc -H 'If-None-Match: "v1-xxxxxxxx"'
curl -i http://127.0.0.1:8080/resources/doc -H 'If-None-Match: W/"v1-xxxxxxxx"'
# 两者都 → 304 Not Modified（响应仍带当前 ETag/Last-Modified）
```

### 乐观并发：If-Match 强比较

```bash
# 客户端拿到 ETag 后再写；弱标签对写永远不够强
curl -i -X PUT http://127.0.0.1:8080/resources/doc \
  -H 'Content-Type: application/json' \
  -H 'If-Match: "v1-xxxxxxxx"' --data '{"text":"A"}'
# 匹配 → 200，ETag 变为 "v2-..."；并发的另一客户端用同一旧标签 → 412

curl -i -X PUT http://127.0.0.1:8080/resources/doc \
  -H 'Content-Type: application/json' \
  -H 'If-Match: W/"v1-xxxxxxxx"' --data '{"text":"B"}'
# 412 PRECONDITION_IF_MATCH_FAILED（If-Match 不接受弱标签）
```

`412` 响应体会带判定轨迹，并附带**当前**校验器，便于客户端刷新重试：

```json
{ "error": {
  "code": "PRECONDITION_IF_MATCH_FAILED",
  "message": "If-Match failed: current version does not strongly match",
  "trace": [{ "step": 1, "header": "If-Match", "comparison": "strong",
             "expected": "\"v1-...\"", "actual": "\"v2-...\"",
             "result": "no-match",
             "basis": "state-changing method: strong comparison per RFC 9110 §8.8.3.2" }] } }
```

### PATCH（RFC 7386）与日期条件

```bash
curl -i -X PATCH http://127.0.0.1:8080/resources/doc \
  -H 'Content-Type: application/merge-patch+json' \
  -H 'If-Match: "v2-..."' --data '{"extra":true,"drop":null}'

curl -i http://127.0.0.1:8080/resources/doc \
  -H 'If-Modified-Since: Wed, 01 Jan 2020 00:00:00 GMT'   # 早于修改 → 200
curl -i -X PUT http://127.0.0.1:8080/resources/doc \
  -H 'Content-Type: application/json' \
  -H 'If-Unmodified-Since: Wed, 01 Jan 2020 00:00:00 GMT' --data '{}'  # 412
```

### 诊断接口（版本历史）

```bash
curl -s http://127.0.0.1:8080/resources/doc/versions        # 全部不可变版本（含墓碑）
curl -s http://127.0.0.1:8080/resources/doc/versions/1      # 指定版本快照
curl -s http://127.0.0.1:8080/resources?limit=50&offset=0   # 集合分页
curl -s http://127.0.0.1:8080/healthz
```

### 状态码速查

| 场景 | 状态码 | `error.code` |
|---|---|---|
| 语法错误的条件头 / JSON | 400 | `MALFORMED_CONDITION_HEADER` / `INVALID_JSON_BODY` |
| GET/HEAD 条件命中（缓存有效） | 304 | — |
| 资源不存在 | 404 | `RESOURCE_NOT_FOUND` |
| 写前置条件失败（含 `If-Match` 打在不存在资源上） | 412 | `PRECONDITION_IF_MATCH_FAILED` / `..._IF_NONE_MATCH_FAILED` / `..._IF_UNMODIFIED_SINCE_FAILED` |
| 创建 / 替换 / 删除成功 | 201 / 200 / 204 | — |
| 未知内部错误 | 500 | `INTERNAL_ERROR`（绝不伪装成成功） |

---

## 5. 日志与可观测性

每个请求可携带 `X-Request-Id`（未提供则自动生成），服务端原样回带，
所有 JSON 日志行用同一 `runId` 关联输入。条件评估逐步记录
`header / comparison / expected / actual / result / basis`，例如：

```json
{"level":"warn","event":"precondition.failed","runId":"client-B",
 "fields":{"code":"PRECONDITION_IF_MATCH_FAILED","trace":[
   {"step":1,"header":"If-Match","comparison":"strong",
    "expected":"\"v1-...\"","actual":"\"v2-...\"","result":"no-match",
    "basis":"state-changing method: strong comparison per RFC 9110 §8.8.3.2"}]}}
```

失败一律为 `warn/error` 且带明确类别，不会把异常或未知状态记为成功。

---

## 6. 测试策略

- **单元测试**：ETag 词法与强/弱比较、HTTP-date、条件优先级与方法分派、
  Merge Patch、ETag 派生与独立预言机一致、内核（基于**独立**内存适配器）。
- **集成测试**：SQLite 持久化/墓碑/分页；**两个 worker 线程独立连接**的
  IMMEDIATE 事务交叉（无 SQLITE_BUSY、无丢失行）；
  **真实监听端口 + 并行 fetch** 的双客户端竞争（200/412、刷新重试、历史 1..3 无洞）。
- **夹具重放**：`tests/fixtures/http-cases.json` 经完整 HTTP 栈逐步断言
  具体状态码、精确 ETag、版本号、响应字段与失败类别（非“接口可调用”）。
  覆盖：双客户端并发、弱标签、通配符、响应丢失后重放、优先级、404 vs 412、畸形头。

```bash
npm test                 # 全部断言
npm run test:coverage    # 含覆盖率（v8）
```

> 说明：RFC 9110 页面在验收环境无法访问，相关规则依据标准条文实现，
> 并在源码注释与测试中标注对应章节号（§8.8.3、§13.1、§13.2.2）。

---

## 7. 验收实测结果

以下命令均在本目录实际执行（Node v22.23.3，npm 10.9.9，Linux）：

- `npm run typecheck`：**通过**（`tsc --noEmit` 无错误）。
- `npm run test:coverage`：**93 个测试全部通过**；覆盖率
  语句 **94.47%** / 分支 **88.62%** / 函数 **95.41%** / 行 **95.76%**（阈值 80%）。
- 真实服务器 + `scripts/demo.sh`：
  - 条件 GET 强标签与 `W/` 弱标签均得 **304**；
  - 两个并发 `If-Match` 写同一旧版本 → **一个 200、一个 412**，412 体含强比较轨迹；
  - `If-None-Match: *` 首次 201、再次 412；
  - GET 不存在 **404**、对不存在资源 `If-Match: *` **412**；
  - 版本历史完整保留 1、2 版本。

如需重新录制实测输出，按第 3 节从 `npm install` 开始即可。
