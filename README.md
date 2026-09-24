# HTTP 分帧一致性验证服务（framesrv）

离线 HTTP/1.1 请求**字节流**解析与分帧一致性验证工具。服务只分析客户端 POST 上来的
原始字节，**不建立任何出站连接、不向真实服务器转发任何请求**。

- 语言：Go（仅标准库 `net/http`，零第三方依赖）
- 支持分帧：`Content-Length`、`Transfer-Encoding: chunked` 子集、无 body
- 严格拒绝请求走私（smuggling）歧义：
  - `Content-Length` 与 `Transfer-Encoding: chunked` 同时出现 → `content_length_and_chunked`
  - 多个取值冲突的 `Content-Length` → `content_length_conflict`（取值相同则接受）
  - `Transfer-Encoding` 仅支持恰好一个 `chunked` 编码（拒绝 `gzip, chunked` 等）
- 严格语法：只接受 CRLF（裸 LF 报 `bare_lf`）、拒绝非法折叠头（`folded_header`）、
  拒绝非十进制 / 带空白 / 带符号的 Content-Length、非十六进制 chunk-size
- 支持 HTTP/1.1 与 HTTP/1.0 请求行、pipeline 连续请求、chunk trailer、chunk extension
- 所有错误返回**流内绝对、0 起始的精确字节偏移**
- 核心验收能力：把同一段字节流在**每个字节边界**切分喂给流式解析器，
  结果必须与一次性解析完全一致（请求内容、错误码、错误偏移三者全等）

## 目录结构

```
cmd/server/main.go            net/http 服务：/parse、/verify、/healthz
internal/parser/parser.go     离线流式解析状态机（精确错误偏移）
internal/parser/consistency.go 全切分 / 随机切分一致性校验器
internal/parser/*_test.go     单元测试 + 全切分验收测试 + fuzz
examples/run_examples.sh      curl 请求样例（精确 CRLF）
examples/output.txt           最近一次实际运行样例的原始输出存档
```

## 依赖

- Go >= 1.23（开发环境实际版本：go1.23.4 linux/amd64）
- 运行/测试零第三方模块；`go.mod` 即为依赖锁定文件（无 `go.sum`，因为没有任何外部依赖）
- 运行示例需要 `curl`（可选 `python3` 用于 JSON 美化，缺失时自动回退原始输出）

## 构建与启动

```bash
go build -o bin/framesrv ./cmd/server
./bin/framesrv -addr :8080
```

参数：

| flag          | 默认值  | 说明                                      |
|---------------|---------|-------------------------------------------|
| `-addr`       | `:8080` | 监听地址                                   |
| `-maxbody`    | 10 MiB  | 单请求解码 body 上限（0 表示不限）          |
| `-maxcarrier` | 1 MiB   | `/parse` 载体请求 body 上限                |
| `-maxverify`  | 4096    | `/verify` 输入上限（全切分校验为 O(n²)）   |

## 测试

```bash
go test ./...              # 全部单元测试与切分一致性验收测试
go test -fuzz=FuzzParser -fuzztime=30s ./internal/parser/   # 模糊测试
```

fuzz 不变量：任意字节流（上限 256B）在任意字节切分下，解析结果与一次性解析完全一致，
且解析器永不 panic。

## HTTP 接口

载体说明：用 POST 把**原始 HTTP/1.1 请求字节流**（必须是真实的 CRLF）作为 body 发送。
这只是传输手段；服务对这些字节做离线分析。解析失败是分析结果，不是服务故障，HTTP 状态码仍为 200。

### POST /parse

解析整段字节流（可含 pipeline 多请求）。

```bash
printf 'POST /a HTTP/1.1\r\nHost: x\r\nContent-Length: 5\r\n\r\nhello' \
  | curl -sS -X POST --data-binary @- http://127.0.0.1:8080/parse
```

成功响应（body 字段为 base64，避免二进制不可读）：

```json
{
  "ok": true,
  "input_bytes": 70,
  "request_count": 1,
  "requests": [
    {
      "method": "POST", "target": "/a", "version": "HTTP/1.1",
      "headers": [{"name": "Host", "value": "x"},
                  {"name": "Content-Length", "value": "11"}],
      "frame": "content-length",
      "content_length": 11,
      "body": "aGVsbG8gd29ybGQ=",
      "body_length": 11,
      "start_offset": 0,
      "raw_length": 70
    }
  ]
}
```

失败响应（精确偏移，0 起始、流内绝对位置；截断错误偏移 = 已收到的总字节数）：

```json
{
  "ok": false,
  "input_bytes": 67,
  "error": {
    "code": "content_length_and_chunked",
    "offset": 37,
    "message": "Content-Length and Transfer-Encoding: chunked must not appear together"
  }
}
```

错误码一览：`bare_lf`、`invalid_request_line`、`unsupported_http_version`、
`invalid_header`、`invalid_field_value`、`folded_header`、`invalid_content_length`、
`content_length_conflict`、`content_length_and_chunked`、`unsupported_transfer_encoding`、
`invalid_chunk_size`、`chunk_data_truncated`、`invalid_chunk_terminator`、
`body_truncated`、`body_too_large`、`incomplete_stream`。

### POST /verify

对同一字节流执行**穷举字节切分**：

```
""|input, input[:1]|input[1:], input[:2]|input[2:], …, input|""
```

共 `len(input)+1` 个切分位置，逐位置比较流式解析与一次性解析的请求数、请求各字段、
错误码和错误偏移。

```bash
printf 'POST /a HTTP/1.1\r\nContent-Length: 11\r\n\r\nhello world' \
  | curl -sS -X POST --data-binary @- http://127.0.0.1:8080/verify
# {"ok":true,"input_bytes":50,"splits_checked":51}
```

- `ok:true`：全部切分位置结果一致；
- `parse_error`：该流本身的解析错误（格式非法≠分帧不一致，例如走私歧义输入在
  所有切分位置都报同一个偏移的错误）；
- `mismatch`：一旦出现切分不一致，给出切分位置与差异明细（正常不应发生）。

### GET /healthz

`{"status":"ok"}`

## 验收场景与实测结果（2026-09-24，go1.23.4）

1. **遍历每个字节切分位置**：`go test ./...` 全部通过；语料含合法 CL/chunked/pipeline、
   截断、裸 LF、折叠头、CL+chunked（两种头顺序）、冲突 CL、非法 chunk-size 等 20+ 条流，
   每条都在全部 `n+1` 个切分位置验证一致；另有逐字节 Feed 测试与随机多刀切分测试
   （20 KiB 流 × 多轮）。
2. **连续请求（pipeline）**：一条流内 bodyless + CL + chunked 依次解析，
   `start_offset/raw_length` 精确衔接。
3. **截断块**：`5\r\nabc` 后 EOF → `chunk_data_truncated`，偏移 = 收到的总字节数；
   chunk 数据后不是 CRLF → `invalid_chunk_terminator`，偏移指向第一个错误字节。
4. **歧义头**：CL+chunked（无论头先后）→ 偏移统一指向 TE 行首；冲突 CL → 指向第二个 CL 行首。
5. **fuzz**：60 秒执行约 60.5 万随机输入，无 panic、无切分不一致。
6. **端到端样例**：`examples/run_examples.sh` 对运行中的服务实际执行 9 个场景全部符合预期，
   原始输出存档于 `examples/output.txt`。

## 明确的范围限制（未做 / 不做）

- 不实现出站连接与反向代理，禁止把解析结果当“净化后的请求”转发（本服务只做分析）。
- 不解析响应报文，只解析请求。
- chunked 只支持单个 `chunked` 传输编码；`gzip`/`identity` 等其它 TE 编码直接拒绝
  （不替用户解码实体内容）。
- 不做 HTTP 语义校验（方法合法性、Host 必填、1.0 keep-alive 规则等不在范围内）。
- `/verify` 为 O(n²)，默认限制 4096 字节；大输入请走库 API `VerifyRandomSplits`。
- 请求头数量 / 单行长度未单独设上限（仅受载体大小限制）。
