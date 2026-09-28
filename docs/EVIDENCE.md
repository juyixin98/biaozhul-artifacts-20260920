# 验证证据（EVIDENCE）

本文件保留**真实执行过的命令与输出结论**，首次使用者可按同样步骤复现。
执行环境：Node.js v22.23.3 / npm 10.9.9 / Linux x86_64，无外部服务依赖。

## 1. 静态类型检查

```bash
$ npm run typecheck   # tsc -p tsconfig.json --noEmit
# 退出码 0，无输出（strict + noUncheckedIndexedAccess + exactOptionalPropertyTypes）
```

## 2. 自动化测试

```bash
$ npm test            # vitest run
 Test Files  10 passed (10)
      Tests  124 passed (124)
   Duration  ~0.9s
```

测试组织（独立单元 / 集成两层）：

| 测试文件 | 层次 | 覆盖要点 |
|---|---|---|
| `test/unit/rangeHeader.test.ts` | 单元 | Range 语法结构、`>2^53` 整数、各类 malformed / unsupported-unit |
| `test/unit/ifRange.test.ts` | 单元 | 强 ETag / 弱 ETag 拒绝 / IMF 日期 / 无法解析 |
| `test/unit/intervals.test.ts` | 单元 | 裁剪、后缀、零长度对象原因分类、重叠+相邻合并、链式合并 |
| `test/unit/httpDate.test.ts` | 单元 | IMF 往返、秒级比较、历史格式宽容、非法输入 |
| `test/unit/multipart.test.ts` | 单元 | 计划长度=实际封装字节；**独立参考解析器**拆包逐字节核验 |
| `test/unit/resolve.test.ts` | 单元 | 全部决策路径、状态码与原因码、部分合法、If-Range、极大 bigint |
| `test/unit/diagnostics.test.ts` | 单元 | 脱敏规则、环形缓冲覆盖与排序 |
| `test/unit/config.test.ts` | 单元 | 环境变量默认/覆盖/启动期校验 |
| `test/unit/sqliteStore.test.ts` | 单元 | 原始字节往返（含 0..255）、bigint size、空对象、文件库持久化 |
| `test/integration/server.test.ts` | 集成 | 真实 Fastify + SQLite；HTTP 状态/头/体逐字节核验、诊断接口、500 兜底 |

关键断言方式（不是“接口能调用”式冒烟）：

- **失败类别**：断言具体原因码，如 `none-satisfiable`、`too-many-ranges`、
  `response-too-large`、`malformed-range`、`if-range-mismatch`、
  `if-range-undecidable`、`start-beyond-size`、`zero-length-object`。
- **零长度对象**：GET → `200 Content-Length: 0`；任何 Range → `416` 且
  `Content-Range: bytes */0`，摘要原因为 `zero-length-object`。
- **相邻/重叠**：`bytes=0-4,5-9`（相邻）合并为单区间非 multipart；
  `bytes=0-9,9-19`（共享字节 9）合并为 `0-19`，内容字节按并集 20 计而非 21。
- **部分合法**：`bytes=0-9,500-600` 在 100 字节对象上 → 206 只回 0-9，
  诊断中 `satisfiableCount=1 / unsatisfiableCount=1` 且原因是 `start-beyond-size`。
- **极大整数**：对象大小 `9007199254740993`（2^53+1），
  `bytes=9007199254740992-9007199254740992` 精确定位末区单字节（若用 Number 会丢精度）。
- **multipart 重组**：测试内置**独立参考解析器**（手工按 boundary 游标切包，
  不 import 任何被测编码代码），逐 part 比对数据与 `Content-Range`，
  再按声明偏移写回缓冲，覆盖位置与原对象逐字节相同、未请求位置保持填充。
- **头体一致性**：每个响应断言 `Content-Length === 实际体长度`，
  诊断记录再做一次交叉校验（`headerBodyConsistent`）。参考期望值来自夹具原始内容，
  而非被测核心自身生成。

## 3. 覆盖率

```bash
$ npx vitest run --coverage
File             | % Stmts | % Branch | % Funcs | % Lines
All files        |   95.2  |  89.01   |  100    |  95.43
 src/server.ts   |   89.36 |  78.94   |  100    |  89.2
 contract/…      |   96.72 |  94.44   |  100    |  98.21
 diagnostics/…   |   97.43 |  84      |  100    |  100
 kernel/…        |   99.15 |  93.68   |  100    |  99.09
 store/…         |  (含于 All files)
```

高于 80% 门槛（vitest 配置 `coverage.thresholds` 强制 lines/functions/statements ≥80、branches ≥70）。
`src/index.ts` 为进程引导，不纳入统计，由下述真实启动验证覆盖。
未覆盖行仅剩两条难注入的防御分支：内核同步抛错兜底、头已写出后的存储异常兜底。

## 4. 种子数据（真实输出）

```bash
$ npm run seed
[seed] id=hello-txt size=19 etag="5784…10a0" -- 小文本对象，19 字节
[seed] id=alphabet size=26 etag="d6ec…49d38" -- A-Z 字母表，26 字节
[seed] id=zero-empty size=0 etag="e3b0…b855"  -- 零长度对象（空内容 SHA-256）
[seed] id=binary-512 size=512 etag="1100…4eb9b" -- 0..255 两轮
[seed] id=padding-10k size=10000 etag="dce9…0854d"
```

## 5. 真实 HTTP 端到端冒烟

```bash
$ npm start                 # http://127.0.0.1:3000，首次启动自动播种
$ bash scripts/smoke.sh
PASS  完整 GET: HTTP 200
PASS  单范围 206: HTTP 206
PASS  开放结尾 206: HTTP 206
PASS  后缀范围 206: HTTP 206
PASS  结束越界裁剪 206: HTTP 206
PASS  起始越界 416: HTTP 416
PASS  零长度对象 416: HTTP 416
PASS  语法错误 400: HTTP 400
PASS  多范围 206: HTTP 206
PASS  相邻合并（非 multipart）206: HTTP 206
PASS  If-Range 匹配 206: HTTP 206
PASS  If-Range 不匹配回 200: HTTP 200
PASS  对象不存在 404: HTTP 404
```

多范围实际线格式（`Range: bytes=0-2,23-25`，头声明 `Content-Length: 230`，
体实测 230 字节）：

```
--rrbbNlFUkemmPMf9fzBa
Content-Type: text/plain; charset=ascii
Content-Range: bytes 0-2/26

ABC
--rrbbNlFUkemmPMf9fzBa
Content-Type: text/plain; charset=ascii
Content-Range: bytes 23-25/26

XYZ
--rrbbNlFUkemmPMf9fzBa--
```

补充人工验证（真实 curl）：

- HEAD `/objects/alphabet` → `200`，无响应体，`content-length: 26`，
  带 `etag` 与 `accept-ranges: bytes`。
- `Range: bytes=9007199254740993-` → `416` + `content-range: bytes */26`（无精度错乱、无崩溃）。
- `If-Range: W/"x"`（弱 ETag）→ `200` 完整表示。
- `Range: bytes=0-9,9-19` → `206`，`content-range: bytes 0-19/26`，体为 `A..T` 共 20 字节。
- GET `zero-empty` → `200` + `content-length: 0`。
- 诊断接口 `/_diagnostics/records/:requestId` 返回结论、原因码、
  解析/可满足/不可满足计数、计划与实际字节、`headerBodyConsistent: true`；
  携带 `Authorization` 时记录中只出现脱敏值（`Bearer top-secret-token-value`
  记录为 `Bea***ue`），原文不落盘。

## 6. 限额配置生效（真实重启验证）

以严格环境变量重启：`RANGE_MAX_RANGES=2 RANGE_MAX_RESPONSE_BYTES=100 RANGE_PORT=3100 npm start`。

```
Range: bytes=0-1,2-3,4-5        → 400 {"code":"too-many-ranges",
                                     "message":"范围规格数量 3 超过上限 2"}
Range: bytes=0-199 (10k 对象)   → 400 {"code":"response-too-large",
                                     "message":"响应总字节 200 超过上限 100"}
Range: bytes=0-49               → 206（未超限）
```

## 7. 复现步骤汇总

```bash
npm install
npm run typecheck
npm test
npx vitest run --coverage
npm run seed && npm start       # 另开终端：bash scripts/smoke.sh
```

结论：124 个自动化测试全部通过，覆盖率 statements 95.2% / branches 89.0% /
functions 100% / lines 95.4%，真实 HTTP 冒烟 13/13 通过，
头体长度在单范围、multipart、HEAD、拒绝、零长度各类响应上均一致。
