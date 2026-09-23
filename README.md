# httpframe — HTTP/1.1 分帧一致性验证服务（纯后端）

一个**离线**的 HTTP/1.1 请求字节流解析服务。它只解析你 POST 上来的原始字节，
**不会向任何真实服务器发起连接或转发流量**。用于验证 Content-Length /
Transfer-Encoding 分帧在任意字节切分（模拟 TCP 段边界）下结果是否一致。

- 语言：Go（module `httpframe`，**零第三方依赖**，仅标准库 `net/http` 等）
- 形态：HTTP JSON 接口 + 可复用的解析库 `frame/`
- 平台：Linux（Go 1.23 开发与验证；`go.mod` 要求 `go 1.23`）

## 支持的协议子集

- 请求行：`METHOD SP request-target SP HTTP/1.1 CRLF`（严格，仅 HTTP/1.1）
- 头部：RFC 7230/9112 字段语法；头部值两侧 OWS 自动裁剪
- 分帧：
  - `Content-Length`：单值；**重复但完全相同**的 CL 头部接受
  - `Transfer-Encoding: chunked`：解析 chunk-size（16 进制）、chunk 扩展
    （`name=token/"quoted string"`）、chunk 数据与 trailer
- HTTP/1.1 流水线（pipelining）：同一字节流中多个连续请求

### 明确拒绝（并返回精确错误偏移）

| 情况 | 错误 code |
|---|---|
| `Content-Length` 与 `Transfer-Encoding` 同时存在 | `content_length_and_transfer_encoding` |
| 多个 `Content-Length` 值冲突 | `content_length_conflict` |
| CL 非数字 / 逗号列表 / 溢出 | `bad_content_length` |
| **非法折叠头**（字段行以 SP/HT 开头，obsolete folding） | `folded_header` |
| 非 `chunked` 的 Transfer-Encoding、多个 TE 头部 | `bad_transfer_encoding` |
| bare LF、行内杂散 CR、行内 NUL | `bare_lf` / `stray_cr` / `nul_byte` |
| 非法请求行 / 头部名 / 缺失冒号 / 非法字段值 | `bad_request_line` / `bad_header` |
| chunk-size 非 16 进制、64 位溢出 | `bad_chunk_size` |
| chunk 扩展语法非法 | `bad_chunk_extension` |
| chunk 数据后不是 CRLF | `bad_chunk_terminator` |
| trailer 中出现 CL/TE | `bad_trailer` |
| 声明体超过上限（默认 10 MiB） | `body_too_large` |
| 流提前结束（**截断的请求/块**） | `truncated` |
| 单消息模式后还有多余字节 | `too_many_messages` |

所有错误都带绝对字节偏移 `offset`（从流的第 0 字节计）。截断错误的偏移
**指向第一个缺失字节**（通常等于已喂入字节总数）。

## 目录结构

```
go.mod                          # 无 require，go.sum 因而为空（零第三方依赖）
frame/decode.go                 # 增量解析器（状态机）+ 一次性 API
frame/verify.go                 # 逐字节切分一致性验证
frame/decode_test.go            # 单元/验收测试
internal/server/server.go       # net/http 接口
internal/server/io.go           # 带大小上限的请求体读取
internal/server/server_test.go  # HTTP 接口测试
cmd/httpframe/main.go           # 服务入口
```

## 启动

```bash
# 需 Go 1.23+；仅标准库，go build 不会下载任何依赖
go run ./cmd/httpframe -addr :8080 -max-body 10485760 -max-input 16777216
```

参数：

- `-addr`：监听地址（默认 `:8080`）
- `-max-body`：单个请求实体上限，字节（默认 10 MiB，`<=0` 不限制）
- `-max-input`：外层 POST 请求体上限（默认 16 MiB）

也可 `go build -o httpframe ./cmd/httpframe` 后运行二进制；离线环境直接
拷贝源码即可，不需要网络。

## HTTP 接口

待解析的原始字节作为**外层 POST 的请求体**（`Content-Type: application/octet-stream`）。
解析器只看这层 body 的原始字节，外层头部不参与解析。

### 1. `POST /parse` — 解析单个请求

加 `?pipeline=1` 则解析流中全部连续请求。

成功：`200`，`{"count":N,"messages":[...]}`；
分帧错误：`422`，`{"code":...,"offset":N,"message":...}`。

每个 message 含：方法/目标、各头部及其行偏移、`header_end`（=实体起始偏移）、
`chunked`、`body_base64`、`body_length`、各 chunk（含扩展/偏移/数据）、
trailers、`end`（下一个请求的起始偏移）。

### 2. `POST /verify-splits` — 逐字节切分一致性验收

对 `0..len(data)` 的**每一个**切分位置 c：先喂 `data[:c]` 再喂 `data[c:]`，
检查两条不变量：

1. 前缀不变量：增量喂前缀 + Close == 一次性解析前缀；
2. 重组不变量：前缀 + 后缀的结果（消息、错误 code、错误绝对偏移）与一次性
   解析整流**完全一致**。

返回 `200 {"ok":true,...}`；只要有一个切分不一致就 `409` 并列出失败位置。
注意：**非法流本身也应当在每个切分位置产生一致的错误**（例如 CL+chunked
无论从哪里切开，最终都报同一个偏移）。

### 3. `GET /healthz`

## 请求样例（curl）

```bash
# (a) Content-Length 正常请求
printf 'POST /a HTTP/1.1\r\nHost: x\r\nContent-Length: 5\r\n\r\nhello' \
  | curl -s -X POST --data-binary @- http://127.0.0.1:8080/parse

# (b) chunked 请求（带扩展与 trailer）
printf 'POST /c HTTP/1.1\r\nTransfer-Encoding: chunked\r\n\r\n5;foo=bar\r\nhello\r\n6\r\n world\r\n0\r\nX-Trailer: v\r\n\r\n' \
  | curl -s -X POST --data-binary @- http://127.0.0.1:8080/parse

# (c) 连续请求（流水线）
printf 'GET /1 HTTP/1.1\r\nHost: a\r\n\r\nGET /2 HTTP/1.1\r\nHost: b\r\n\r\n' \
  | curl -s -X POST --data-binary @- 'http://127.0.0.1:8080/parse?pipeline=1'

# (d) CL 与 chunked 并存 → 422，offset 指向后出现的冲突头部
printf 'POST / HTTP/1.1\r\nContent-Length: 1\r\nTransfer-Encoding: chunked\r\n\r\nx\r\n0\r\n\r\n' \
  | curl -s -X POST --data-binary @- http://127.0.0.1:8080/parse

# (e) 冲突长度 → 422，offset 指向第二个 CL 行首
printf 'POST / HTTP/1.1\r\nContent-Length: 2\r\nContent-Length: 3\r\n\r\nab' \
  | curl -s -X POST --data-binary @- http://127.0.0.1:8080/

# (f) 非法折叠头（第二行以 TAB 开头）→ 422，offset 指向该 TAB
printf 'POST / HTTP/1.1\r\nContent-Length: 2\r\n\tX: y\r\n\r\nab' \
  | curl -s -X POST --data-binary @- http://127.0.0.1:8080/parse

# (g) 截断的 chunk（声明 5 字节只给 2 字节）→ 422 truncated
printf 'POST / HTTP/1.1\r\nTransfer-Encoding: chunked\r\n\r\n5\r\nhe' \
  | curl -s -X POST --data-binary @- http://127.0.0.1:8080/parse

# (h) 对正常流做逐字节切分一致性验收
printf 'POST /a HTTP/1.1\r\nContent-Length: 11\r\n\r\nhello world' \
  | curl -s -X POST --data-binary @- http://127.0.0.1:8080/verify-splits

# (i) 对 CL+chunked 歧义流做切分验收（非法但每个切分结果必须一致）
printf 'POST / HTTP/1.1\r\nContent-Length: 1\r\nTransfer-Encoding: chunked\r\n\r\nx\r\n0\r\n\r\n' \
  | curl -s -X POST --data-binary @- http://127.0.0.1:8080/verify-splits
```

## 自动化测试

```bash
go test -race -cover ./...
```

覆盖：精确错误偏移、CL 与 chunked 并存拒绝、冲突长度、折叠头、bare LF/CR/NUL、
截断块、流水线、**11 条流（合法/截断/歧义）的逐字节切分一致性遍历**、
多段随机分片、HTTP 层 422/409/413 等。

## 已实测结果

> 见仓库内 `RUNLOG.md`（测试与 curl 示例的真实运行记录）。

## 设计取舍与已知边界

- 只支持 HTTP/1.1；HTTP/1.0 请求行直接拒绝（避免 1.0 下 CL 缺省语义歧义）。
- 请求方法必须是 token；请求目标不允许空白/DEL（origin-form / asterisk-form /
  absolute-form / authority-form 均可通过，不做更细的形式区分）。
- 头部值允许 HTAB、VCHAR 与 obs-text（>=0x80），拒绝其它控制字符与 DEL；
  请求行不允许 obs-text。
- `Transfer-Encoding` 只接受精确的 `chunked`（大小写不敏感）；
  `chunked, gzip`、`gzip, chunked` 等组合一律拒绝（请求走私高风险面）。
- chunk 扩展按 RFC 的 BWS/token/quoted-string 语法严格解析并原样记录，
  不解析其语义。
- 默认 10 MiB 实体上限与 16 MiB 输入上限，防止内存放大；可用参数放宽/关闭。
- 纯文本 JSON 报告；响应体用 base64 承载以保证任意字节无损往返。
