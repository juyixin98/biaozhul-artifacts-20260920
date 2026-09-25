# 实际运行记录（RUNLOG）

环境：Linux 6.8.0-90-generic x86_64，cargo 1.98.1 / rustc 1.98.1
（2026-09-23 实际执行；以下输出均为真实命令输出，未通过项为零。）

## 1. 构建

```text
$ cargo build --release
   Compiling http-framing v0.1.0
    Finished `release` profile [optimized] target(s) in 0.98s
```

`cargo build`（debug）与 `cargo clippy --all-targets` 均**零警告**。

## 2. 自动化测试

```text
$ cargo test
src/lib.rs 单元测试 .............. 5 passed, 0 failed
framer_behaviour.rs ............. 10 passed, 0 failed
server_tcp.rs（真实 TCP）........ 7 passed, 0 failed
split.rs（字节切分验收）......... 5 passed, 0 failed
合计 27 passed, 0 failed
```

`cargo test --release` 结果相同（27/27）。

验收核心测试输出的切分位置计数：

```text
validated 3497 byte-cut positions across the corpus
```

即对语料库中每个输入，在 `k = 0..=len` 的**每个**字节位置切成两段喂入，
共 3497 个切分点，外加“每次 1 字节”和不规则多片两组调度，
全部与整包喂入结果一致；非法流在任意切分下都给出同一 `ErrorKind`。
`all_error_kinds_exercised_except_none` 保证 22 种错误类型全部被语料覆盖。

## 3. 样例请求（TCP 服务，服务端 read-size=1）

启动：

```text
$ ./target/release/http-framing-server --addr 127.0.0.1:18091 --read-size 1
http-framing reference service listening on http://127.0.0.1:18091
```

`scripts/send_samples.sh 127.0.0.1:18091`（批量）与
`--bytewise`（客户端每字节一个 TCP 段）两种方式结果完全一致：

| 样例 | 结果 |
|---|---|
| 01_get_minimal | `200`，`X-Framing: none` |
| 02_fixed_length | `200`，`X-Framing: content-length`，body 11 字节 |
| 03_chunked_trailer | `200`，`X-Framing: chunked`，解码 body `Wikipedia`（9 字节），2 个 trailer |
| 04_pipeline | 一条连接上 **3 个** `200`（targets `/one` `/two` `/three`） |
| 05_reject_cl_te | `400` `X-Frame-Error: te-with-content-length` |
| 06_reject_te_cl | `400` `te-with-content-length` |
| 07_reject_duplicate_cl | `400` `duplicate-content-length` |
| 08_reject_obs_fold | `400` `obsolete-line-folding` |
| 09_reject_bad_chunk | `400` `chunk-terminator` |
| 10_reject_ambiguous_ws | `400` `invalid-header-name` |
| 11_reject_bare_lf | `400` `bad-line-ending` |
| 12_truncated | `400` `incomplete`（EOF 时请求未完成） |

流水线样例的独立计数核验：

```text
200 responses on one connection: 3
targets seen: [b'/one', b'/two', b'/three']
```

chunked 样例的 stdio 实际响应：

```text
$ cat samples/03_chunked_trailer.http | ./target/release/http-framing-server --stdio
HTTP/1.1 200 OK
Content-Type: application/json
Content-Length: 235
X-Framing: chunked
Connection: keep-alive

{"method":"POST","target":"/upload","headers":[["Host","example.com"],
["Transfer-Encoding","chunked"]],"framing":{"mode":"chunked"},
"body_hex":"57696b697065646961","body_len":9,
"trailers":[["ETag","\"deadbeef\""],["X-Checksum","ok"]]}
```

## 4. 命令行覆盖上限（真实执行）

```text
$ printf 'POST / HTTP/1.1\r\nHost: x\r\nContent-Length: 100\r\n\r\n' \
    | ./target/release/http-framing-server --stdio --max-body-bytes 3
HTTP/1.1 413 Payload Too Large
X-Frame-Error: body-too-large

$ printf 'GET /aaaaaaaaaaaaaaaa HTTP/1.1\r\n\r\n' \
    | ./target/release/http-framing-server --stdio --max-request-line 10
HTTP/1.1 431 Request Header Fields Too Large
X-Frame-Error: request-line-too-large

$ printf 'POST / HTTP/1.1\r\nTransfer-Encoding: chunked\r\n\r\n10\r\n' \
    | ./target/release/http-framing-server --stdio --max-chunk-size 8
HTTP/1.1 413 Payload Too Large
X-Frame-Error: chunk-too-large
```

## 5. 未通过项 / 已知边界

- 测试与手工验证中**无未通过项**（27/27，clippy 零警告）。
- 已知范围边界（按需求刻意排除，非缺陷）：
  - 不解析 HTTP/1.0、绝对 URI 形式请求行、CONNECT；
  - 不支持 chunk 扩展（`;…`）、非 `chunked` 的传输编码、逗号形式多 CL；
  - 服务端不是代理：不转发、不实现内容协商/主机路由，只回报分帧结构；
  - 一个连接在首个分帧错误后只回一个 4xx 即关闭，不在同一连接上恢复解析
    （避免把歧义字节当作后续请求）。
