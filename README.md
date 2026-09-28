# Range 读取后端（本地不可变对象）

面向**本地不可变对象**的字节范围（HTTP Range）读取后端，支持单范围与
`multipart/byteranges` 多范围。TypeScript + Node.js + Fastify + SQLite（better-sqlite3）。
全部数据来自仓库内确定性合成夹具，无任何外部账号或真实业务数据依赖。

## 特性与语义

依据 RFC 9110 第 13/14 章实现：

- **单范围形式**：显式区间 `bytes=0-499`、开放结尾 `bytes=500-`、后缀范围 `bytes=-500`。
- **越界处理**：结束偏移越界自动裁剪到对象末尾；起始偏移越界返回 `416` 并带
  `Content-Range: bytes */<size>`。
- **零长度对象**：普通 GET 返回 `200` 空体；任何 Range 请求返回 `416`。
- **多范围**：返回 `multipart/byteranges`；先按起点排序，再合并**重叠与相邻**区间
  （相邻合并是明确策略：拆开只浪费封装字节）。
- **部分合法 vs 全不合法**：一个请求中部分规格可满足时，只返回可满足部分，
  诊断记录同时保留可满足/不可满足计数及每个不可满足规格的分类原因；全部不可满足才 `416`。
- **If-Range**：强 ETag 或 HTTP 日期；弱 ETag（`W/"…"`）与无法解析的值按
  “无法判定”处理；不匹配或无法判定都回退 `200` 完整表示（绝不报错）。
- **限额**：`RANGE_MAX_RANGES` 限制规格数量（合并前计数）；
  `RANGE_MAX_RESPONSE_BYTES` 限制响应总字节（multipart 时含封装开销，超限 `400`）。
- **偏移精度**：全链路用 `bigint` 计算偏移，支持超过 `2^53` 的极大整数；
  内容以**未压缩原始字节**存入 SQLite，字节偏移永远针对原表示，不存在压缩后猜测。
- **HEAD**：实体头（含 Content-Length）与 GET 一致，响应无体。

## 模块职责

| 模块 | 职责 |
|---|---|
| `src/contract/` | **契约解析**：Range / If-Range 头的纯语法解析，不接触对象大小与 I/O |
| `src/kernel/` | **执行内核**：语义裁剪、区间合并、限额判断、If-Range 预检、multipart 长度规划与编码；全部纯函数、bigint |
| `src/store/` | **状态适配**：SQLite 不可变对象仓储，原始字节 BLOB 读写、ETag 计算 |
| `src/diagnostics/` | **诊断接口**：环形决策记录、请求标识、敏感头脱敏 |
| `src/server.ts` | Fastify 传输层：只做 HTTP 适配，判定全部委托内核 |
| `fixtures/` | 确定性合成样例对象（种子与测试共用） |
| `test/unit` `test/integration` | 独立单元测试 / HTTP+SQLite 集成测试 |

## 快速开始

需要 Node.js ≥ 20（开发环境为 Node 22）。

```bash
npm install
npm run seed          # 可选：显式播种到 data/objects.db（首次启动也会自动播种）
npm start             # http://127.0.0.1:3000
```

冒烟验证（另开终端，或直接运行 `bash scripts/smoke.sh`）：

```bash
curl -s http://127.0.0.1:3000/objects                         # 对象清单
curl -s -D- -o /dev/null -H 'Range: bytes=0-4' http://127.0.0.1:3000/objects/alphabet
curl -s -H 'Range: bytes=0-2,23-25' http://127.0.0.1:3000/objects/alphabet
curl -s -H 'Range: bytes=999-' -D- http://127.0.0.1:3000/objects/alphabet   # 416
curl -s "http://127.0.0.1:3000/_diagnostics/records?limit=5"
```

## 样例数据（`fixtures/samples.ts`，确定性生成）

| id | 大小 | 用途 |
|---|---|---|
| `hello-txt` | 19 | 小文本 |
| `alphabet` | 26 | A–Z，后缀/越界/多范围 |
| `zero-empty` | 0 | 零长度对象 |
| `binary-512` | 512 | 0..255 两轮，逐字节偏移核验 |
| `padding-10k` | 10000 | 总量限额、大偏移 |

所有对象 Last-Modified 固定为 `Fri, 02 Jan 2026 03:04:05 GMT`，ETag 为原始字节 SHA-256。

## HTTP 接口

### `GET|HEAD /objects/:id`

| 场景 | 状态 | 关键响应头 |
|---|---|---|
| 无 Range / Range 覆盖整对象 / If-Range 不匹配 | 200 | `ETag` `Last-Modified` `Accept-Ranges: bytes` |
| 单范围 | 206 | `Content-Range: bytes s-e/size` |
| 多范围（合并后 ≥2 段） | 206 | `Content-Type: multipart/byteranges; boundary=…` |
| Range 语法错误 / 超数量 / 超总量 | 400 | JSON 错误包络 |
| 全部范围不可满足（含零长度对象） | 416 | `Content-Range: bytes */size` |
| 对象不存在 | 404 | JSON 错误包络 |

错误包络：`{"success":false,"error":{"code":"…","message":"…","requestId":"…"}}`。
可通过 `X-Request-Id` 请求头自定义请求标识，响应头与诊断记录都会回显。

### 诊断接口

- `GET /_diagnostics/records?limit=N` —— 最近 N 条决策记录（新到旧）。
- `GET /_diagnostics/records/:requestId` —— 按请求标识回查单条记录。

每条记录说明**结论（full/partial/rejected/not-found）、原因码、关键状态**
（解析规格数、可满足/不可满足计数及分类原因、计划总字节、实际体字节、
Content-Length 是否与实际体一致）。请求头经白名单过滤，
`Authorization`/`Cookie`/`X-Api-Key` 等敏感头只记录脱敏值，对象内容永不入诊断。

## 配置

全部通过环境变量（见 `.env.example`）：`RANGE_HOST` `RANGE_PORT` `RANGE_DB_PATH`
`RANGE_MAX_RANGES` `RANGE_MAX_RESPONSE_BYTES` `RANGE_DIAGNOSTICS_CAPACITY`。
非法值在启动期快速失败。

## 测试

```bash
npm test                 # 全部测试（vitest）
npm run test:unit        # 仅单元测试
npm run test:integration # 仅集成测试
npm run typecheck        # tsc 严格类型检查
npx vitest run --coverage # 覆盖率
```

测试要点（不是“接口能调用”式冒烟）：

- 单元测试直接断言解析结构、状态码、**失败类别原因码**、bigint 精确值。
- 零长度对象、相邻范围、边界重叠（共享一个字节）、超过 `2^53` 的极大整数均有覆盖。
- multipart 测试用**独立参考解析器**手工按线格式拆包（不复用任何被测编码函数），
  逐字节核对每个 part 数据与 `Content-Range`，并重组回对象偏移位置核验。
- 每个响应都断言 `Content-Length` 与实际体长度相等；诊断记录二次交叉校验。

最近一次真实运行结论见 [docs/EVIDENCE.md](docs/EVIDENCE.md)。
