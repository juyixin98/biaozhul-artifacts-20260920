# Local Immutable-Object Range Backend

本地不可变对象的 HTTP Range 读取后端，支持单范围与 `multipart/byteranges` 多范围。
技术栈：**TypeScript · Node.js（内置 `node:sqlite`）· Fastify**。
所有数据均为本地合成夹具，无任何生产账号或真实业务数据依赖。

实现遵循 [RFC 9110](https://www.rfc-editor.org/rfc/rfc9110) §13（条件请求）与 §14（范围请求）。

---

## 1. 环境要求

- Node.js **>= 22.5**（使用内置实验性模块 `node:sqlite`；所有 npm 脚本已带 `--experimental-sqlite`，无需额外安装数据库）
- npm 10+（随 Node 提供）

检查：

```bash
node --version   # v22.x
```

## 2. 首次运行（30 秒上手）

```bash
npm install        # 安装 fastify / typescript / tsx
npm run seed       # 生成确定性合成对象到 data/objects.db
npm start          # 启动服务（编译后）或 npm run dev（tsx 直跑源码）
```

启动后：

```
Range backend on http://127.0.0.1:3000 (db=data/objects.db, maxSpecs=50, maxResponseBytes=16777216, mergeGap=1)
Endpoints: GET /objects/:id, GET /diagnostics, POST /objects/:id
```

真实请求示例：

```bash
$ curl -s -D - http://127.0.0.1:3000/objects/alpha -H 'Range: bytes=5-9'
HTTP/1.1 206 Partial Content
content-type: text/plain; charset=utf-8
content-range: bytes 5-9/26
content-length: 5

fghij
```

多范围：

```bash
$ curl -s -D - http://127.0.0.1:3000/objects/alpha -H 'Range: bytes=0-2,20-25' -o /dev/null
HTTP/1.1 206 Partial Content
content-type: multipart/byteranges; boundary=RANGE-00bf2346319397dda2fdb5bc
content-length: 263
```

全部不可满足：

```bash
$ curl -s -D - http://127.0.0.1:3000/objects/alpha -H 'Range: bytes=999-'
HTTP/1.1 416 Range Not Satisfiable
content-range: bytes */26
```

### 内置样例对象（`npm run seed`）

| id            | 大小     | 说明                                   |
| ------------- | -------- | -------------------------------------- |
| `empty`       | 0 字节   | 零长度对象边界                         |
| `hello`       | 19 字节  | `"Hello, Range world!"`                |
| `alpha`       | 26 字节  | `a`–`z`                                |
| `binary-256`  | 256 字节 | 依次包含每个八位组 `0x00`–`0xFF`       |
| `blob-120k`   | 120 KiB  | 确定性字节图案，用于大对象/预算测试    |

也可以用 POST 写入自己的本地对象（不可变，重复 id 返回 409）：

```bash
curl -s -X POST --data-binary 'my bytes' \
  -H 'Content-Type: text/plain' \
  http://127.0.0.1:3000/objects/custom
```

## 3. Range 语义（已实现的策略）

| 场景 | 行为 |
| --- | --- |
| `bytes=a-b`（闭区间） | `b` 超过对象末尾时**钳制**到 `size-1`，仍然可满足 |
| `bytes=a-`（开放结尾） | 一直取到最后一个字节 |
| `bytes=-N`（后缀） | 取最后 `min(N, size)` 字节；`N >= size` 时返回整个表示，**不是错误** |
| `bytes=-0` | 丢弃（`EMPTY_SUFFIX`） |
| 首字节位置 `>= size` | 该规格丢弃（`START_BEYOND_OBJECT`） |
| **部分合法** | 合法部分照常 `206`，非法规格记入诊断 `dropped[]` |
| **全不合法** | `416 Unsatisfiable`，带 `Content-Range: bytes */size` |
| 零长度对象上的任何范围 | `416`，`Content-Range: bytes */0`（普通 GET 仍是 `200` 空体） |
| 语法错误（如 `bytes=5-2`、`bytes=abc`） | `400`，响应体给出出错规格的下标与原因（区别于 416） |
| 非 `bytes` 单位（如 `items=…`） | 按 RFC 忽略，回退 `200` 完整表示 |
| 极大整数（40 位及以上） | 用 BigInt 解析：超大后缀→整个对象；超大首字节→416，**不会**报整数语法错 |
| 相邻范围 `[0,9],[10,19]` | 按 `RANGE_MERGE_GAP=1` 合并为 `[0,19]`，多部分由此塌缩为单部分 |
| 重叠/包含/重复 | 合并，字节只发一次；诊断同时保留合并前 `requestedBytes` 与合并后 `servedBytes` |
| 范围数量上限 | 原始规格数超过 `RANGE_MAX_SPECS` → `400 TOO_MANY_RANGES`（在钳制/合并之前计数） |
| 总响应量上限 | 单范围看载荷；多范围把**帧装开销上界**也算入预算，超限 → `400 RESPONSE_SIZE_LIMIT_EXCEEDED` |

**字节偏移基准**：对象只以恒等编码（不压缩）存储，ETag 为字节内容的 SHA-256。
所有偏移都基于**原对象字节**；多范围由状态层按原偏移用 SQLite `substr()` 逐段读出，
不存在"基于压缩后长度猜测偏移"的问题。

### If-Range

| If-Range 值 | 结果 |
| --- | --- |
| 与当前强 ETag 完全相等 | 正常 `206` |
| 强 ETag 不匹配 | 回 `200` 完整表示（`IF_RANGE_MISMATCH`） |
| 弱验证符 `W/"…"` | 弱比较永远不授权范围 → `200` 完整表示 |
| 与 Last-Modified 同秒的 HTTP-date | `206` |
| 过期日期 | `200` 完整表示 |
| 既非合法 ETag 也非可解析日期（如 `???`） | **无法判定** → 保守地回 `200`（`IF_RANGE_INDETERMINATE`，与硬不匹配区分） |

## 4. 诊断接口

每次请求都产生一条结构化记录，带 **request id** 与关键状态，说明为什么
接受、拒绝或无法判定。每个响应都回 `X-Request-Id` 头，可用于关联。

```bash
$ curl -s 'http://127.0.0.1:3000/diagnostics?limit=1'
# 记录含: requestId, decision(accepted:single|accepted:multipart|accepted:full|rejected),
#         status, code, reason, intervals, requestedBytes, servedBytes,
#         mergeCount, dropped[{index,raw,reason}], rangeHeader ...

$ curl -s http://127.0.0.1:3000/diagnostics/<X-Request-Id>
$ curl -s 'http://127.0.0.1:3000/diagnostics?objectId=alpha&decision=rejected'
```

记录同时：保存在有界内存环形缓冲（诊断接口数据源）、镜像到 stdout、
追加写入 JSONL 文件（`DIAGNOSTICS_LOG`，可置空关闭文件）。

**敏感数据**：默认绝不记录任何对象字节。设置 `LOG_BODIES=1` 时也只附加
**脱敏后的结构预览**——字母数字一律掩码为 `#`，二进制八位组变 `×`，仅保留
标点与长度形状；例如 `Hello, Range world!` 记录为 `#####, ##### #####!`，
原始内容不会出现在日志里。Range 头超过 512 字符会被截断标注。

## 5. 模块划分（各有真实职责，非单文件脚本）

```
src/
  types.ts                 # 领域类型（区间、规格、决策、限制…）
  config/index.ts          # 12-factor 配置解析与启动期校验（纯函数，可测）
  range/
    parser.ts              # 契约解析：Range 头 + If-Range 条件（纯语法，BigInt）
    intervals.ts           # 执行内核的几何层：按尺寸解析 + 排序合并（纯函数）
    core.ts                # 执行内核：把解析/条件/限制/决策编排成明确裁决（无 I/O）
    multipart.ts           # multipart/byteranges 帧装（按原偏移逐段切片）
  state/store.ts           # 状态适配：SQLite BLOB 存储、ETag、substr() 偏移读取、不可变约束
  diagnostics/logger.ts    # 诊断：内存环 + 文件 JSONL + 脱敏
  http/routes.ts           # HTTP 适配：头文本<->内核裁决，只做翻译不发明策略
  app.ts                   # 装配真实适配器栈
  index.ts                 # 启动入口 / 优雅停机
scripts/
  seed.ts                  # 确定性合成夹具入库
  make-samples.ts          # 生成 samples/ 与含字面预期答案的 manifest
  verify-samples.ts        # 独立核验线上响应（自写帧装 + 字面哈希比对）
test/
  fixtures/reference.ts    # 独立参考：手写 multipart 解析器、字面夹具（不 import 被测内核）
  unit-node/               # 单元测试（parser/intervals/core/multipart/store/diagnostics/config）
  integration/             # 集成测试（真实 Fastify + :memory: SQLite，经 HTTP 注入）
```

依赖方向：`http → range / state / diagnostics → config / types`；
`range` 内核不依赖任何 I/O，因此裁决可被穷举式单测。

## 6. 测试与证据

### 运行测试

```bash
npm test             # 全部：单元 + 集成
npm run test:unit    # 仅单元
npm run test:integration
npm run typecheck    # tsc --noEmit
```

最近一次真实输出：

```
# tests 124
# suites 0
# pass 124
# fail 0
# cancelled 0
```

带覆盖率（Node 内置 c8 风格报告）：

```bash
node --experimental-sqlite --import tsx --experimental-test-coverage \
  --test test/unit-node/*.test.ts test/integration/*.test.ts
```

`src/` 行覆盖率约 **99%**（`all files` 行 98.98% / 分支 89.69% / 函数 98.40%），
其中 `range/core`、`range/intervals`、`range/multipart` 均为 100/100/100。

### 证据如何做到"独立"且"断言具体结果"

- **答案不来自被测核心**：`test/fixtures/reference.ts` 提供一个**从零手写**的
  multipart 线格式解析器（靠扫描分隔符、读取每部分自己的 `Content-Range` 定长），
  以及字面夹具；所有预期字节都直接从夹具切片或写字面串。
- **重组响应逐字节核验**：集成测试把 multipart 响应交给独立解析器重组，再逐字节
  比对原对象对应区间，并用 `0x00–0xFF` 全八位组对象排查传输损坏。
- **响应头与实际体长度一致**：每个 206 都断言 `Content-Length === rawPayload.length`。
- **断言具体结果与失败类别**：精确到状态码（200/206/400/416 区分）、`Content-Range`
  字面值、区间数组、`dropped[].reason`（`START_BEYOND_OBJECT` / `EMPTY_SUFFIX` /
  `OBJECT_EMPTY`）、拒绝码（`UNSATISFIABLE_RANGE` / `TOO_MANY_RANGES` /
  `RESPONSE_SIZE_LIMIT_EXCEEDED` …），而非只断言"接口可调"。
- **要求的边界场景全部覆盖**：零长度对象、相邻范围、边界重叠（共享字节）、
  40 位极大整数、后缀/开放结尾/越界、部分合法 vs 全不合法、If-Range 各分支。

### 独立样例核验

```bash
npm run make:samples     # 生成 samples/*.bin + manifest.json（字面哈希答案）
npm run verify:samples   # 启动栈，把线上响应与 manifest 逐字节/逐哈希比对
```

`verify-samples` 对多范围响应**在脚本里自行重新帧装**预期部分后与实际体逐字节比较
（不调用被测的 `multipart` 编码器），单范围则比对 manifest 的字面 SHA-256。
最近一次结果为 `All sample checks passed.`（含 394 字节多范围体的逐字节比对）。

## 7. 配置

见 `.env.example`。可用环境变量：

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `HOST` / `PORT` | `127.0.0.1` / `3000` | 监听地址 |
| `DB_PATH` | `data/objects.db` | SQLite 文件（集成测试用 `:memory:`） |
| `DIAGNOSTICS_LOG` | `data/requests.jsonl` | JSONL 诊断文件；置空只保留接口与 stdout |
| `LOG_BODIES` | `0` | `1` 时附加脱敏结构预览（永不输出原始字节） |
| `RANGE_MAX_SPECS` | `50` | 单个请求允许的范围规格数 |
| `RANGE_MAX_RESPONSE_BYTES` | `16777216` | 总响应量上限（多范围含帧装上界） |
| `RANGE_MERGE_GAP` | `1` | 合并间隙：`1` 连相邻也合并，`0` 只合并严格重叠 |

非法配置（非整数、负数、超过安全整数）在启动期快速失败并指出变量名。

## 8. 可用 npm 脚本

```bash
npm run dev             # tsx 直跑源码
npm run build           # 编译到 dist/
npm start               # 运行编译产物
npm run seed            # 写入确定性合成对象
npm run make:samples    # 生成样例数据 + 预期清单
npm run verify:samples  # 独立逐字节核验样例
npm test                # 全部测试
npm run typecheck       # 类型检查
```

> 说明：`node:sqlite` 在 Node 22 带实验性警告，所有脚本已内置
> `--experimental-sqlite`，功能在本地验证完整。
