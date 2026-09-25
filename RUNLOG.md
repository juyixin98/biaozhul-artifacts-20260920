# 运行记录（RUNLOG）

本文件如实记录在本机实际执行过的命令与结果。日期：2026-09-23。
环境：Linux 6.8.0-90-generic (x86_64)；`rustc 1.98.1` / `cargo 1.98.1`；
Python 3.12.3。项目零第三方 crate 依赖、零 `unsafe`。

## 1. 构建

```text
$ cargo build
    Finished `dev` profile [unoptimized + debuginfo] target(s)

$ cargo build --release
    Finished `release` profile [optimized] target(s)
$ ls -l target/release/http-framing-server
-rwxrwx-rx 1 admin admin 571240 ... target/release/http-framing-server
```

（早期构建中出现过的编译错误与修复过程见第 5 节。）

## 2. 自动化测试

```text
$ cargo test
 Running unittests src/lib.rs                                  ... 0 passed
 Running unittests src/bin/http-framing-raw.rs                 ... 0 passed
 Running unittests src/bin/http-framing-server.rs              ... 0 passed
 Running tests/byte_split.rs      test result: ok. 24 passed; 0 failed
 Running tests/framer_unit.rs     test result: ok. 54 passed; 0 failed
 Running tests/samples_test.rs    test result: ok.  2 passed; 0 failed
 Running tests/server_e2e.rs      test result: ok. 11 passed; 0 failed
```

合计 **91 项集成/端到端测试全部通过，0 失败、0 忽略**（库本身无 `#[cfg(test)]`
内联单测，测试全部位于 `tests/`，避免与差分工具循环引用）。

```text
$ cargo clippy --all-targets
（无输出：0 warning / 0 error）

$ cargo fmt --check
（退出码 0）
```

### 字节位置切分验收（核心项）

`tests/byte_split.rs`（24 项）对每个目标输入：

1. 一次性喂入取得参考结果（帧序列，或唯一错误类型）；
2. 在 `0..=len` 的**每一个字节位置**切成 `[..i]`、`[i..]` 两次喂入并比对；
3. 逐字节喂入（1 字节/次）比对；
4. 用 6 个固定种子（1/2/3/42/1337/0xdeadbeef）各 4 组伪随机多段切分比对。

`tests/samples_test.rs` 另外对 `samples/` 全部 31 个样例做整段与每个字节位置
切分（含逐字节）比对。所有断言通过——**分帧结论与 TCP 分段无关**。

覆盖的输入类别（节选）：

- 流水线：两个 GET、GET→定长 POST→chunked（带 trailer）→GET 四连、
  第一请求合法第二请求为重复 CL（错误偏移仍须稳定）；
- chunked：多块、体内含字面 CRLF（RFC 7230 示例
  `Wikipedia in\r\n\r\nchunks.`）、quoted-string 扩展、trailer 多字段、
  50 个小块；
- 走私歧义：CL.TE、TE.CL、重复 CL（等值/不等值）、CL 冲突列表、
  obs-fold、冒号前空白、TE 多编码、块数据后缺 CRLF、块大小与数据不符；
- 超限：请求行、头部区、声明主体、块主体（小限额 `Limits` 下复测）；
- 长输入：4 KiB 定长主体做全切分点 + 逐字节。

## 3. 真实 TCP 服务手工验证

```text
$ ./target/release/http-framing-server --bind 127.0.0.1:19090 --idle-timeout 3
listening on 127.0.0.1:19090
```

（注：先尝试的 18080 端口已被机器上的另一个 HTTP 服务占用，二进制如实报错
`server error: Address already in use (os error 98)`，换到空闲的 19090。）

用 Python 裸 socket 发送字节，实际收到的响应（原样摘录）：

```text
=== plain GET ===
HTTP/1.1 200 OK
{"type":"frame","seq":1,"method":"GET","target":"/hello","framing":"none", ...}

=== fixed POST ===
HTTP/1.1 200 OK
..."framing":"fixed-length","content_length":5,... "body":"hello", ...}

=== chunked+trailer ===
HTTP/1.1 200 OK
..."framing":"chunked", ... "body":"hello","trailer_count":1,
 "trailers":[["X-A","1"]], ...}

=== pipelined x2 (one write) ===
HTTP/1.1 200 OK {"...","target":"/a","seq":1, ...}
HTTP/1.1 200 OK ... "/b","seq":2 ...        # 同一连接按序两条响应

=== CL.TE smuggle ===
HTTP/1.1 400 Bad Request
{"type":"error","status":400,"error":"te_and_cl","offset":26}

=== bare LF ===
HTTP/1.1 400 Bad Request
{"type":"error","status":400,"error":"bare_line_feed","offset":29}

=== dup CL ===
HTTP/1.1 400 Bad Request
{"type":"error","status":400,"error":"duplicate_content_length","offset":26}

=== oversize header (发送 70KiB，默认 64KiB 上限) ===
HTTP/1.1 400 Bad Request
{"type":"error","status":400,"error":"header_section_too_large","offset":65561}
```

随后用 `samples/` 下全部 31 个样例对运行中的服务做整段回放与**逐字节回放**
（每字节间隔 0.5 ms），以 HTTP 状态码汇总：

```text
whole: 31/31 status matches; byte-by-byte: 31/31
（14 个合法样例 → 200；17 个非法样例 → 400/413，逐字节结果完全一致）
```

逐字节回放脚本在发送裸 LF 样例时观察到一次 `BrokenPipeError`：这是**预期
行为**——服务器在读到非法字节时立刻回 400 并关闭连接，客户端仍在慢速写入，
内核因此返回 RST。脚本对该情况容错后重跑，31/31 一致。

附带用自带的回放客户端验证（替代 `nc`，字节不经任何重编码）：

```text
$ ./target/release/http-framing-raw --bind 127.0.0.1:19090 \
      samples/05-chunked-simple.http samples/24-cl-te.http
### samples/05-chunked-simple.http (93 bytes)
HTTP/1.1 200 OK ... "framing":"chunked" ... "body":"hello world" ...
### samples/24-cl-te.http (71 bytes)
HTTP/1.1 400 Bad Request
{"type":"error","status":400,"error":"te_and_cl","offset":17}
```

端到端测试 `tests/server_e2e.rs`（11 项）同样启动真实二进制、绑定随机端口
（`--bind 127.0.0.1:0`，从 stderr 的 `listening on …` 解析实际端口），
全部通过。

## 4. 样例语料

```text
$ python3 tools/gen_samples.py
wrote 31 samples to .../samples
```

14 个合法（01–14）+ 17 个非法（20–36），每个 `.http` 为线上精确字节
（CRLF、控制字符都按字节保留），`samples.json` 记录字节数、预期
（accepted/rejected）与确切错误码。非法样例覆盖裸 LF/CR、obs-fold、
冒号前空白、CL.TE/TE.CL、重复/冲突 CL、TE 多编码/重复、CL 非数字、
坏请求行、HTTP/1.0、坏块大小、trailer 藏 CL、坏 chunk-ext、无头部分隔符。

## 5. 开发过程中实际发现并修复的问题（如实记录）

以下问题均由 `cargo build` / `cargo test` 暴露，修复后复测通过：

1. **编译错误**：`ParseError` 的 Display 误用 `kind`（应为 `self.kind`）；
   闭包返回切片生命周期不满足；`std::env::ArgsSkip` 类型不存在；
   `header_offsets` 字段漏声明。均改正后通过编译。
2. **字段值把空格误判为非法**：初版 `is_field_value_byte` 用 VCHAR
   （0x21–0x7E）漏了 0x20 SP，导致所有含内部空格的值被拒。改为
   `SP/HTAB/VCHAR/obs-text`（仍不含 CR/LF）。
3. **`trim_ows` 在全 OWS 串上切片下溢**（`slice index starts at 1 but
   ends at 0`），改为 `rposition` 实现。
4. **chunk-ext 校验器要求多余的前导 `;`**，使 `5; name="a b"` 被误拒；
   重写为“首个分号之后即扩展名”的状态校验（含 token/quoted-string/多扩展）。
5. **跨 TCP 分段时错误字节偏移漂移 1 字节**：行起始偏移最初取“本次调用的
   位置”，一行被切开时就不稳定。新增 `pending_line_start`，仅在累加器为空
   （新行第一字节）时记录，保证偏移是绝对流偏移。新增测试
   `error_offsets_stable_under_chunking` 逐切分点锁定该性质。
6. **样例 08 的生成器把 `\r\n` 写进了会再做一次 LF→CRLF 转换的字符串**，
   拼出非法块序列（实测报 `MalformedChunkSize @ 84`）。改为手工字节拼接
   四个块；修复后样例与切分测试通过。
7. **一个端到端用例的走私负载本身写错**（在块数据后放了任意文本），改为
   经典形态“声明块大小 4、实际 5 字节”，使第 5 字节落在块后必须为 CR 的
   位置而被拒，并断言其后的 `GET /smuggled` 永远不会被处理。

clippy 早期还提示过若干风格项（`is_some` 后 `unwrap`、手工去前缀、
`repeat().take()` 可写为数组重复、未使用导入等），均已清理至零告警。

## 6. 截至交付时的状态

- `cargo test`：**91/91 通过，0 失败**。
- `cargo clippy --all-targets`：零告警；`cargo fmt --check`：通过。
- 真实服务手工/脚本回放：31/31 样例状态码符合预期，整段与逐字节一致。
- **已知未通过项：无。** 明确不实现的范围（代理转发、HTTP/1.0、其它传输
  编码、TLS、前端、`Expect: 100-continue` 等）见 README 第 1 节，属于
  预先划定的子集边界而非缺陷。
