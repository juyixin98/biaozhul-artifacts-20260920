# multipart/form-data 接收后端

TypeScript + Node.js (≥22) + Fastify + SQLite（better-sqlite3）从零搭建的流式
`multipart/form-data` 接收服务。支持普通字段部件与**受限文件部件**，在终止边界
验证通过前不落库，失败时只回收本请求临时数据。

## 快速开始

```bash
npm ci                 # 依赖锁定安装（package-lock.json, lockfileVersion 3）
npm run dev            # 启动 http://127.0.0.1:3000（tsx 直跑 TS）
# 或
npm run build && npm start
```

环境变量（均有默认值）：`PORT` `HOST` `DATA_DIR` `DB_PATH` `TEMP_DIR` `RUN_LOG`，
以及限额 `MAX_PART_BYTES` `MAX_FIELD_BYTES` `MAX_TOTAL_BYTES`
`MAX_HEADER_BYTES` `MAX_PARTS`。

## 复现全部结论

```bash
npm run golden:generate   # 重新生成确定性黄金字节（应与已提交文件逐字节一致）
npm test                  # 104 个测试：单元 / 黄金校验 / 仓储 / HTTP 端到端
npm run test:coverage     # 附带覆盖率（行 97% / 分支 88% / 函数 95%）
npm run build             # tsc 生产构建到 dist/
bash examples/upload-curl.sh                 # curl 正常/异常调用示例
PORT=3100 npm run dev & node examples/upload-client.mjs   # 零依赖跨块客户端
```

真实 HTTP 正常/异常运行结果见 [`reports/live-results.md`](reports/live-results.md)，
夹具说明见 [`fixtures/README.md`](fixtures/README.md)。

## HTTP 接口

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/uploads` | multipart 接入；原始流透传进解析内核 |
| GET | `/submissions` | 最近提交（不含 BLOB） |
| GET | `/submissions/:id` | 单次提交及其部件元数据 |
| GET | `/submissions/:id/parts/:partId/blob` | 下载已存储文件字节 |
| GET | `/diagnostics/runs` | 最近运行记录（内存环） |
| GET | `/diagnostics/runs/:runId` | 单次运行的完整中间状态（可重放） |
| GET | `/health` | 存活探针 |

成功响应：`{ success:true, data:{ runId, submission } }`；
失败响应：`{ success:false, error:{ errorClass, code, message, details, runId? } }`。

## 四类可区分失败（输入 / 状态 / 资源 / 计算）

| errorClass | HTTP | 典型 code |
|------------|------|-----------|
| INPUT_ERROR | 400 | NOT_MULTIPART, MISSING_BOUNDARY, MALFORMED_BOUNDARY, MISSING_TERMINATOR, BARE_LF, MALFORMED_HEADER, DUPLICATE_HEADER, MISSING_CONTENT_DISPOSITION, MISSING_FIELD_NAME, UNSUPPORTED_HEADER, BAD_HEADER_ENCODING, FILENAME_REJECTED, EMPTY_BODY |
| STATE_CONFLICT | 409 | PARSER_FINISHED, ALREADY_ABORTED, NOT_TERMINATED, PART_NOT_OPEN |
| RESOURCE_LIMIT | 413 | PART_TOO_LARGE, FIELD_TOO_LARGE, TOTAL_TOO_LARGE, HEADER_TOO_LONG, TOO_MANY_PARTS, BOUNDARY_TOO_LONG |
| COMPUTE_ERROR | 500 | TEMP_IO_ERROR, DB_ERROR |

## 默认限额与上传限制

- 单文件 5 MiB、单字段 64 KiB、**单请求聚合 16 MiB**、头段 8 KiB、最多 32 部件、
  boundary 最长 70（RFC 2046 bchars）。
- 文件扩展名白名单（`.txt/.log/.csv/.json/.png/.jpg/.jpeg/.gif/.pdf`）与
  Content-Type 白名单双校验；拒绝控制字符、路径分隔符、`..` 段与 dotfile。

## 工程边界（模块与契约）

```
src/protocol/                 契约解析 + 执行内核（无 fs/sqlite 依赖）
  errors.ts                   四类错误契约 + 状态码映射
  config.ts                   限额 / 限制配置
  charset.ts                  RFC 7578 / RFC 5987 文件名编码规则
  params.ts                  ;key="value" 参数（引号/转义/重复检测）
  content-type.ts             multipart 信封与 boundary 校验
  headers.ts                  部件头解析 + 上传限制
  parser.ts                   流式边界状态机（PartSink 接口）
src/storage/                  状态适配
  temp-store.ts               每请求隔离临时目录；字段内存 / 文件 spool
  repository.ts               SQLite 单事务原子提交（submissions/parts/part_blobs）
  upload-service.ts           请求生命周期编排（解析→终止校验→提交→清理）
src/server/
  diagnostics.ts              runId 运行记录（JSONL + 内存环）
  app.ts                      Fastify 路由与统一错误信封
  index 入口 = src/index.ts
```

数据契约集中在 `src/protocol/types.ts`：`PartMeta → PartSink.write/end/destroy
→ CompletedPart → ParsedForm → CommittedSubmission`；错误统一为
`MultipartError(errorClass, code, details)`。

## 关键正确性点

- **边界跨块**：扫描时保留 `needle.length + 1` 尾字节，任何可能跨写的分隔符都不会
  被切断；解析器内部用串行 pump 链杜绝 write/finish 重叠。
- **近似边界不误切**：`CRLF "--" boundary` 必须再看后随 2 字节——`--` 为终止、
  `CRLF` 为普通边界，其它一律按正文继续扫描（见黄金向量 v03）。
- **终止后可见**：仅在 close-delimiter（`--boundary--`）及其后 CRLF/EOF 验证后才
  `end()` 当前部件并提交；缺终止符→`MISSING_TERMINATOR`，无任何落库。
- **失败清理隔离**：每请求独立目录，失败/取消只删本目录；FileSink 未 finalize 才
  删半成品文件，finalize 后留给提交读取，提交后 finally 统一回收。
- **头部规则**：同名部件头/同名 disposition 参数重复即拒；`filename*` 优先于
  `filename`；显式拒绝过时的 RFC 2047 encoded-word。
