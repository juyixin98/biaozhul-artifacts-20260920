# RUNLOG — 实际运行记录

- 日期：2026-09-23
- 环境：Linux 6.8 x86_64，Go 1.23.1（标准库，零第三方依赖）
- 说明：以下均为本机真实执行输出；端口因 18080 被占用，改用 **19090**。

## 1. 依赖锁定

`go.mod` 无任何 `require`，`go mod tidy` 后**不产生 `go.sum`**（无第三方
模块可校验）。依赖即为所用 Go 版本的标准库本身（`net/http`、`encoding/json`、
`encoding/base64`、`bytes`、`io` 等），离线可构建：

```
$ go mod tidy
$ cat go.mod
module httpframe

go 1.23
$ ls go.sum
ls: cannot access 'go.sum': No such file or directory   # 预期：无第三方依赖
```

## 2. 构建与静态检查

```
$ go build ./...            # 通过，无输出
$ go vet ./...              # 通过，无输出
$ gofmt -l .                # 无输出（全部已格式化）
```

## 3. 自动化测试

命令：`go test -race -cover -count=1 ./...`

```
?   httpframe/cmd/httpframe       [no test files]
ok  httpframe/frame                coverage: 82.8% of statements
ok  httpframe/internal/server      coverage: 80.2% of statements
```

`go test -v` 统计：**47 个 PASS（含子测试），0 个 FAIL**。顶层用例：

frame 包：
- TestParseContentLength / TestParseNoBody
- TestParseChunked / TestParseChunkedNoTrailers（含 chunk 扩展与 trailer）
- TestPipeline（连续 3 请求，校验每条 start/end 偏移）
- TestSingleRejectsTrailingBytes（多余字节偏移指向第二个请求）
- TestErrorOffsets（24 个子用例，逐条断言 code 与精确 offset）
- TestSplitConsistencyAcceptance（**11 条流 × 每个字节切分位置**，
  含合法流、截断流、CL+chunked 歧义流、折叠头、冲突长度、bare LF）
- TestRandomSegmentation（不规则多段分片喂入，结果与一次性解析一致）

server 包：healthz、CL 解析、pipeline、CL+chunked→422 偏移、折叠头→422
偏移、截断 chunk→422、405、413 超限、verify-splits 正常流/歧义流。

## 4. 启动与接口示例（真实输出摘要）

启动：

```
$ go build -o /tmp/httpframe ./cmd/httpframe
$ /tmp/httpframe -addr :19090
2026/09/23 23:10:49 httpframe listening on :19090 (max-body=10485760, max-input=16777216)
$ curl -s http://127.0.0.1:19090/healthz
{"status":"ok"}
```

(a) Content-Length：`POST /parse` → 200；body_base64=`aGVsbG8=`（"hello"），
`header_end`/`body_start`=48，`end`=53。

(b) chunked（`5;foo=bar` + `6` + `0` + trailer）→ 200；
body_base64=`aGVsbG8gd29ybGQ=`（"hello world"，11 字节），返回 2 个 chunk
的 size/扩展/数据偏移与 1 个 trailer（X-Trailer）。

(c) 流水线：`GET /1` + `GET /2` → `count=2`，第二条 `start`=28 恰为第一条
`end`=28。

(d) CL+chunked 并存：
```
HTTP 422
{"error":"Content-Length and Transfer-Encoding must not appear together",
 "code":"content_length_and_transfer_encoding","offset":36}
```
offset 36 = `Transfer-Encoding:` 行首（后出现的冲突头部）。

(e) 冲突长度（CL:2 后 CL:3）：
```
HTTP 422
{"error":"conflicting Content-Length values: 2 vs 3",
 "code":"content_length_conflict","offset":36}
```
offset 36 = 第二个 CL 行首。

(f) 非法折叠头（`\r\n\tX: y`）：
```
HTTP 422
{"error":"obsolete line folding (leading OWS) is not allowed",
 "code":"folded_header","offset":36}
```
offset 36 = 那个 TAB 字节本身。

(g) 截断 chunk（声明 5 字节只给 `he`）：
```
HTTP 422
{"error":"unexpected end of stream: incomplete message",
 "code":"truncated","offset":52}
```
offset 52 = 流长度，即第一个缺失字节。

(h)(i)(j) 逐字节切分验收：

| 流 | total_bytes | cuts_checked | 结果 |
|---|---|---|---|
| 正常 CL 流 | 51 | 52（=N+1） | 200 `ok:true` |
| CL+chunked 歧义流 | 74 | 75 | 200 `ok:true` |
| 截断 chunked 流 | 52 | 53 | 200 `ok:true` |

即：无论从哪个字节切开喂入，最终消息、错误 code 与错误**绝对偏移**都与
一次性解析完全一致。

## 5. 未完成项 / 边界（如实记录）

- 仅覆盖 HTTP/1.1 请求方向；未实现响应解析、HTTP/1.0、TLS。
- Transfer-Encoding 只接受精确 `chunked`；`identity`、TE 列表组合、
  `gzip` 等内容编码一律拒绝（有意收窄走私面）。
- chunk 扩展做语法校验与原样保留，不解释参数语义。
- 未做 `Expect: 100-continue`、连接级 keep-alive 超时等传输语义（本服务
  只解析一次性 POST 上来的字节，不维护真实连接状态）。
- 18080 端口在本机已被其他服务占用，示例改用 19090；功能不受影响。
