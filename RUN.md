# 实际运行记录（RUN）

本文件记录在本机**实际执行**的命令与结果。环境：

```text
$ rustc --version
rustc 1.98.1 (48a229cea 2026-09-01)
$ cargo --version
cargo 1.98.1 (797e8a9bc 2026-08-05)
OS: Linux 6.8.0-90-generic (x86_64)
```

> 结论先行：**全部 55 个自动化测试通过，dev / release 两个 profile 下
> `cargo clippy --all-targets` 均为 0 警告，真实 TCP 上的 curl、内置客户端、
> 三场景续流演示均按预期工作。** 开发过程中发现并修复了 2 个真实代码缺陷
> （见文末「过程中发现并修复的问题」，如实记录）。

---

## 1. 构建

```text
$ cargo build --release
   Compiling sse-resume v0.1.0
    Finished `release` profile [optimized] target(s) in 1.20s

$ cargo clippy --release --all-targets   # 统计 ^warning:|^error 的行数
0
$ cargo clippy --all-targets             # dev profile
0
```

零外部依赖（`Cargo.toml` 中无 `[dependencies]`），无 `unsafe`
（`src/lib.rs` 有 `#![forbid(unsafe_code)]`）。

## 2. 自动化测试

```text
$ cargo test
```

| 测试二进制 | 用例数 | 结果 |
|---|---:|---|
| 库内单元测试（`decode` 26 + `encode` 2） | 28 | ✅ 全部通过 |
| `tests/chunking.rs`（CR/LF/CRLF 跨块、逐字节、随机分块） | 9 | ✅ |
| `tests/reconnect.rs`（真实 TCP 续流 / reset / HTTP 错误） | 10 | ✅ |
| `tests/roundtrip.rs`（编码↔解码往返、帧注入、模糊） | 8 | ✅ |
| **合计** | **55** | **0 失败** |

汇总行（实际输出）：

```text
test result: ok. 28 passed; 0 failed; ...   (src/lib.rs)
test result: ok.  9 passed; 0 failed; ...   (tests/chunking.rs)
test result: ok. 10 passed; 0 failed; ...   (tests/reconnect.rs)
test result: ok.  8 passed; 0 failed; ...   (tests/roundtrip.rs)
```

关键用例与验收点的对应关系：

| 验收要求 | 对应测试 |
|---|---|
| CR 跨块 | `chunking::cr_splits_everywhere`（在**每个**字节切分点切两块 + 随机分块） |
| LF 跨块 | `chunking::lf_splits_everywhere` |
| CRLF 跨块（`\r` 与 `\n` 分属两块不重复断行） | `chunking::crlf_splits_everywhere`、`crlf_boundary_right_at_chunk_end_all_offsets` |
| 三种终止符混用 | `chunking::mixed_terminators_split` |
| 空 id（清空游标） | `decode::empty_id_clears_last_id_to_empty_string`、`chunking::empty_id_variants_split` |
| 注释（纯 `:`、无空格、心跳） | `decode::comment_lines_are_ignored`、`chunking::comments_and_blank_blocks_split` |
| 多行 data | `decode::multiline_data_joined_with_lf` |
| retry / id / event 语义 | `decode::retry_only_accepts_ascii_digits`、`id_is_recorded_and_persists_across_events` 等 |
| 长度上限与错误类型 | `decode::{line,data,id}_too_long_is_reported`、`invalid_utf8_in_data_value_is_reported`、毒化断言 |
| 断线重连无遗漏 | `reconnect::replay_after_disconnect_has_no_gaps` |
| 允许的重复边界 | `reconnect::duplicate_boundary_when_cursor_lags_one` |
| 过期游标 → 明确 reset | `reconnect::expired_cursor_returns_explicit_reset_then_live_resumes` |
| 非法 / 超前 / 空游标 | `reconnect::{non_numeric,empty_cursor,cursor_beyond_latest}*` |
| reset 后对齐游标无缝继续 | 同 `expired_cursor_...` 后半段、`live_events_after_replay_do_not_duplicate` |
| HTTP 404 / 405 | `reconnect::http_errors_for_wrong_path_and_method` |

## 3. 真实 TCP 运行：curl

服务端（历史窗口 3，tick 200ms）：

```text
$ ./target/release/sse-server --port 19095 --history 3 --tick-ms 200
```

无游标订阅（只收实时事件）：

```text
$ curl -sN --max-time 1.0 http://127.0.0.1:19095/events
retry: 3000
: connected
id: 4
event: tick
data: {"ts":1790148271710}

id: 5
event: tick
data: {"ts":1790148271910}
...
```

过期游标（1 已滚出窗口=3，立即 reset 再实时）：

```text
$ curl -sN -H 'Last-Event-ID: 1' http://127.0.0.1:19095/events
id: 8
event: reset
data: {"reason":"cursor-expired","yourLastEventId":"1","earliestId":6,"latestId":8,"resumeAfter":8}

id: 9
event: tick
data: {"ts":1790148272710}
...
```

非法游标：

```text
$ curl -sN -H 'Last-Event-ID: abc' http://127.0.0.1:19095/events
event: reset
data: {"reason":"cursor-invalid","yourLastEventId":"abc","earliestId":9,"latestId":11,"resumeAfter":11}
```

## 4. 真实 TCP 运行：三场景续流演示

```text
$ ./target/release/demo-reconnect
== 演示服务启动于 127.0.0.1:37463，历史窗口=20 ==

--- 场景 1：断线重连，无遗漏 ---
客户端第一次连接收到: id=1,2,3
!! 模拟断线（丢弃连接），客户端记住 Last-Event-ID=3
断线期间服务端又发布 id=[4, 5, 6, 7]
重连后补收到 id=[4, 5, 6, 7]
✔ 无遗漏：补回的正是断线期间的全部事件，且无重复

--- 场景 2：游标落后 1（at-least-once 重复边界）---
客户端实际已收到 id=8，但游标只持久化到 7（崩溃在更新游标前）
重连后服务端重投 id=8
✔ 允许的重复：边界事件 8 被再投递一次，消费方需幂等去重

--- 场景 3：游标已过期（滚出有限历史窗口）---
旧游标 1 重连，收到: event=reset, id=Some("33")
         data={"reason":"cursor-expired","yourLastEventId":"1","earliestId":14,"latestId":33,"resumeAfter":33}
✔ 明确重置：客户端据此做全量再同步，而不会误以为没有缺口

== 全部演示断言通过 ==
```

## 5. 内置客户端（小块读取 + 物理断线 + 带游标重连）

收满即干净退出（exit code 0；`--chunk 16` 强制小读取块压跨块）：

```text
$ ./target/release/sse-client --url http://127.0.0.1:19093/events \
      --max-events 5 --reconnects 2 --chunk 16
[client] 第 1 次连接 ... (Last-Event-ID: None)
[client] 服务端提示重连等待 3000ms
{"event":"tick","id":"4", ...}  (共 5 行)
[client] 已收到 5 个事件，退出      # 退出码 0
```

物理断线测试（`kill -9` 服务端，再同端口重启）中，客户端读到连接结束后
携带最后游标重试，`stderr` 实际记录：

```text
[client] 服务端关闭连接(EOF)，本次收到 6 个事件（累计 6）
[client] 1s 后重连…
[client] 第 2 次连接 ... (Last-Event-ID: Some("9"))
[client] 连接失败: Connection refused (os error 111)
[client] 1s 后重连…
[client] 第 3 次连接 ... (Last-Event-ID: Some("9"))
```

观察：客户端在整个过程中**始终携带最后已知 id（9）**；因服务端重启窗口与
`--reconnects 2` 限制，两次重试发生在新服务起来之前而放弃——这是参数/时序
导致的预期终止，不是崩溃。若调多重连次数或先起服务，可重连成功（库层面的
重放正确性已由 `tests/reconnect.rs` 覆盖）。

---

## 6. 过程中发现并修复的问题（如实记录）

开发中并非一次通过，以下是被测试/真实运行抓出来、随后修复的问题：

1. **连续 CR（`\r\r`）解析错误（真实解析 bug）**
   初版在“上一字节为 CR、当前字节非 LF”时，把当前字节直接塞进新行缓冲，
   导致 `\r\r`（两个空行）里的第二个 CR 被当成内容。`tests/chunking.rs`
   的“在每个切分点切两块”不变量测试抓出此问题。修复：当前字节**重新走
   正常字节流程**（`normal_byte` 尾递归一层，`pending_cr` 已清零，不会无限递归）。

2. **非阻塞 listener 的连接继承非阻塞模式（真实服务 bug）**
   为优雅退出把监听 socket 设为 nonblocking，accept 出的连接继承该模式，
   处理线程却按阻塞 I/O 使用，真实运行 `demo-reconnect` 时第一次读即
   `WouldBlock (os error 11)`。修复：accept 成功后、spawn 处理线程**之前**
   立即 `set_nonblocking(false)`；同时客户端读循环对 `WouldBlock` 容错重试。

3. **解码器毒化后未前置短路**
   报过一次长度错误后，继续 `push` 会在残留行缓冲上再次报 `LineTooLong`
   而非承诺的 `Poisoned`。修复：`push` 入口检查 `poisoned` 立即返回
   `DecodeError::Poisoned`。

4. 若干测试自身的构造/断言错误（长度计数 15→9、历史窗口太小导致预期重放
   变成合法 reset、Vec 索引越界等），均已按规范语义修正为正确断言；这些是
   测试问题，非产品代码缺陷。

## 7. 未通过项 / 已知限制

* 当前**没有**未通过的测试（55/55 通过）。
* 已知非功能限制（设计使然，非缺陷）：服务历史仅存内存，进程重启即清空
  （重连会收到 reset）；阻塞 I/O + 每连接一线程，定位为本地正确性测试床，
  非高并发生产服务器；不做鉴权/压缩/HTTP-2/前端。详见 README「已知边界」。
