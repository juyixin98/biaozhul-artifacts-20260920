# HTTP 范围响应语义（Byte-Range Service）

一个纯后端实验项目：用 Go 实现本地 HTTP 服务，按 RFC 9110 提供不可变二进制制品的
`Range` / `If-Range` 范围响应；配套一个可注入故障的客户端，重试、重组并逐字节核验
下载结果，输出结构化 JSON 报告。

**所有外部依赖均为本进程假服务**（内存制品库、内存对象存储、脚本化故障传输层、
可控假时钟）。项目不连接、也不适合连接任何生产系统；没有前端。

## 功能与语义

- 不可变制品（immutable artifact）：构造时复制字节，派生强校验器，之后只读。
- 范围针对**选定表示**（selected representation）：
  - `identity`（原始字节）与 `gzip` 两种表示，各自有独立强 ETag 与长度；
  - `Range` 偏移、`Content-Length`、`Content-Range` 的总数均按实际选定表示计算；
  - gzip 响应带 `Content-Encoding: gzip` 与 `Vary: Accept-Encoding`。
- 三种字节范围形式：
  - 前缀/闭区间 `bytes=0-99`；
  - 后缀 `bytes=-64`（最后 64 字节；请求长度超过表示时回退为整个表示）；
  - 开区间 `bytes=200-`（到表示末尾）；闭区间右端越界自动夹紧（clamp）。
- 多范围：`multipart/byteranges`，每段独立 `Content-Range`；重叠/重复范围在服务端
  排序合并（coalesce），相邻但不重叠的范围保留为多个分段。
- 正确的状态码与响应头：
  - `206 Partial Content` + `Content-Range`（单范围）或 `multipart/byteranges`（多范围）；
  - `416 Range Not Satisfiable` + `Content-Range: bytes */<size>`（无任何可满足范围，
    包括对零长度制品的任何范围请求）；
  - 语法非法的 `Range` 被忽略，回 `200` 完整表示（RFC：忽略无法解析的范围头）；
  - `If-Range` ETag 不匹配、弱校验器、或日期早于 Last-Modified 时，忽略范围回 `200`；
  - 范围数量超过上限（默认 5）回 `400`；
  - 恒定输出 `Accept-Ranges: bytes`。
- 故障注入客户端：
  - 脚本化传输错误、503/500、响应体中途截断；
  - 指数退避重试（最多 4 次尝试），退避由**可控时钟**驱动，测试零真实等待；
  - 按 `Content-Range` 绝对偏移重组单段与 multipart 响应；
  - 追踪每个字节偏移：缺字节、矛盾重叠字节、长度不符、SHA-256 不符均判定失败；
  - 重组后对原始字节做 SHA-256 核验，输出结构化报告（每次尝试、每个分段、结论）。

非 200/206 响应为 JSON 错误体，例如：

```json
{"error":"requested range not satisfiable","status":416}
```

服务端还会在 `X-Range-Decision` 头中明示本次裁决：
`full` / `single-partial` / `multipart` / `unsatisfiable-416` /
`ignored-malformed` / `ignored-if-range` / `rejected-range-limit`，
便于测试直接观察行为而非猜测。

## 目录结构

```
cmd/
  rangeserver/        本地 HTTP 服务（内置确定性示例制品）
  verify/             进程内验收命令：14 个场景，输出结构化 JSON
internal/
  clock/              Clock 接口：RealClock + 可手动推进的 FakeClock
  blobstore/          进程内假对象存储 + 故障装饰器（FlakyStore）
  artifact/           不可变制品、identity/gzip 选定表示、强 ETag、注册表
  rangespec/          Range 解析/求解/合并、Content-Range、multipart 装配、
                      If-Range 判定、Accept-Encoding 协商
  server/             RFC 9110 范围语义 HTTP 处理器
  fault/              脚本化故障 http.RoundTripper
  client/             重试客户端、重组、覆盖/重叠校验、SHA-256 核验、JSON 报告
examples/             原始 HTTP 报文样例与 curl 脚本
```

## 构建与运行

需要 Go 1.22+，无第三方依赖（仅标准库）。

```bash
# 构建全部
go build ./...

# 启动本地服务（默认 127.0.0.1:8080）
go run ./cmd/rangeserver -addr 127.0.0.1:8080
```

服务启动时内置三个制品：

| ID | 大小 | 内容 |
|----|------|------|
| `binary256` | 256 | 字节 0..255，便于核对偏移 |
| `lorem300` | 300 | 重复文本 `ABCDEFGHIJ` × 30 |
| `empty` | 0 | 零长度制品（验收项） |

### API

| 方法与路径 | 说明 |
|---|---|
| `GET /artifacts` | 制品目录（id、ETag、大小、SHA-256、Last-Modified） |
| `GET /artifacts/{id}` | 取表示，支持 `Range`、`If-Range`、`Accept-Encoding` |
| `HEAD /artifacts/{id}` | 同 GET 但无响应体（含正确 `Content-Length`） |
| `POST /artifacts/{id}` | 实验用：上传字节创建新制品（上限 16 MiB） |
| `GET /healthz` | 健康检查 |

请求样例见 [`examples/requests.md`](examples/requests.md)，可直接执行的脚本见
[`examples/curl-examples.sh`](examples/curl-examples.sh)。

## 验收命令

`cmd/verify` 在**同一进程内**用 `httptest` 启动假服务并驱动全部验收场景，
不产生端口依赖、不访问外部系统：

```bash
go run ./cmd/verify            # 彩色缩进 JSON，退出码 0 表示全部通过
go run ./cmd/verify -pretty=false > report.json
```

覆盖的验收场景（14 个）：

1. 前缀范围；2. 后缀范围；3. 开区间范围与越界夹紧；4. 三范围相邻拼装（multipart）；
5. **重叠范围**合并后重组核验；6. **不满足范围 → 416 + `bytes */256`**；
7. **零长度制品**（范围 416 + `bytes */0`，普通 GET 200 空体）；
8. **If-Range ETag 不匹配 → 200 完整表示**；9. If-Range 日期形式（未来/过去）；
10. 非法 Range 被忽略；11. **范围数量限制 → 400**；
12. 两次传输故障后重试成功（假时钟退避）；13. 响应体截断后重试成功；
14. gzip 选定表示（独立长度/ETag、范围按编码长度计数）。

客户端重组后核验原始字节体现在每个 client 驱动场景的报告字段：
`complete_coverage`、`segments[].sha256`、`actual_sha256 == expected_sha256`、
`verified: true`。

## 测试

```bash
go vet ./...
gofmt -l .   # 应无输出
go test -race -cover ./...
```

实际运行命令、结果与未通过项的记录见 [RUNLOG.md](RUNLOG.md)。

## 设计说明与取舍

- **范围针对选定表示**：gzip 表示携带与 identity 不同的强 ETag，避免用资源数据的
  偏移去描述编码后字节；客户端对 identity 与 gzip 两种下载分别支持（gzip 先整段
  重组再 gunzip 后核验原始字节）。
- **重叠范围**：HTTP 允许服务端将重叠区间合并为一个 206 区间。本服务排序后只合并
  真正重叠/重复的区间；恰好相邻的区间保持独立分段，因此“分片铺满”仍会走
  multipart 路径，测试可覆盖多段重组。
- **范围数量上限**：防止用大量小区间放大 multipart 装配开销；服务端默认 5，
  可用 `server.WithMaxRanges(n)` 调整，客户端有相同的快速失败限制。
- **故障与时间**：网络故障只存在于 `fault.ScriptedTransport` 的脚本中；退避等待走
  `clock.Clock` 接口，测试注入 `FakeClock` 并用后台推进泵释放等待，全程确定性、
  无真实 sleep。
- **安全边界**：仅监听本机、内存态、无鉴权需求；POST 限制 16 MiB；不使用任何
  第三方库。
