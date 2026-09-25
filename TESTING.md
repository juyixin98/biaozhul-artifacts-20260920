# 测试记录（如实）

环境：Linux 6.8.0、`cargo 1.98.1` / `rustc 1.98.1 (2026-09-01)`、edition 2021、
**零第三方依赖**（仅 std）。本机 127.0.0.1 TCP。

所有命令均在项目根目录实际执行；下列输出为真实截取。时间戳随机器不同，结果为准。

---

## 1. 构建

```bash
cargo build --release
# Finished `release` profile [optimized] target(s)
```

`cargo build` / `cargo build --release` 均**无编译警告**（曾出现的 unused-import /
let-chain（2021 不支持）等均已修正）。

---

## 2. 自动化测试 `cargo test`

为查时序不稳定（flaky），连续完整运行 **3 次**，结果一致：

```
test result: ok. 17 passed; 0 failed; 0 ignored  (src 单元测试)
test result: ok.  3 passed                       tests/bounds.rs
test result: ok.  5 passed                       tests/cancel.rs
test result: ok.  4 passed                       tests/concurrency.rs
test result: ok.  8 passed                       tests/wire.rs
```

合计 **37 个测试通过，0 失败**。（另含 1 个 doc 示例，用 `no_run` 编译检查，不计入
执行数。）

覆盖到的验收点：

| 验收点 | 测试 |
|---|---|
| 半包（逐字节） | `decode::byte_by_byte_half_packets`、`wire::server_handles_byte_at_a_time_half_packet` |
| 粘包 | `decode::sticky_packets_*`、`wire::server_handles_glued_frames` |
| 随机任意切分 | `decode::random_splits_always_reassemble`（1..7 字节，200 帧） |
| 子集解析 | `decode::header_subset_parse_exposes_routing_fields`（只读头部） |
| 长度上限 / 仅凭头拒绝 | `decode::length_cap_*`、`wire::payload_length_over_cap_closes_connection` |
| 错误类型逐个命名 | `decode::bad_version_unknown_kind_flags_are_named_errors`、`bad_magic_*` |
| 超时后迟到响应 | `cancel::late_response_after_timeout_is_identified_and_dropped`（late=1，连接仍可用） |
| 取消竞争 | `cancel::cancel_wins_race_*`、`repeated_cancel_race_always_resolves_exactly_once`（40 线程，两种结局都出现） |
| 响应先到再取消 | `cancel::cancel_after_response_already_local_returns_that_response` |
| ID 未释放不复用 | `cancel::request_id_is_never_reused_*`（同槽位代次 +1，完整 id 不再出现） |
| 同连接并发/乱序 | `concurrency::concurrent_requests_*`、`many_concurrent_requests_all_routed_correctly`（60） |
| 内存有界/背压 | `bounds::client_enforces_inflight_cap_with_backpressure`、`server_rejects_over_inflight_*`、`long_run_keeps_inflight_table_empty_between_bursts` |
| 错误帧后连接处理 | `wire::bad_magic_*`、`corrupted_crc_*`、`payload_length_over_cap_*`（关闭+EOF）；`concurrency::application_error_keeps_connection_alive`（应用错误不关连接） |
| 未知 id 的 CANCEL | `wire::cancel_for_unknown_id_is_acknowledged_and_connection_lives` |
| CRC 正确性 | `crc32::known_vectors`（空串/`123456789`=0xCBF43926 等）+ 与 python `zlib.crc32` 现场互通交叉验证 |

---

## 3. 端到端实际运行（release 二进制 + 真实 TCP）

启动：`BRPC_NO_STDIN=1 ./target/release/brpc-server 127.0.0.1:9011`
（`LISTEN 127.0.0.1:9011` 已确认。）

### 3.1 客户端演示 `brpc demo`

```
-- out-of-order: fast responses should print before slow --
id=0000000100000001 ... (utf8: first-fast)
id=0000000100000002 ... (utf8: second-fast)
id=0000000100000000 ... (utf8: later)          # 慢请求最后才回，按 id 正确归属
-- timeout, then late response --
id=0000000200000000 timed out waiting (request still running server-side)
late responses identified after drop: 1         # 迟到响应被识别
-- cancel race --
id=...00000000 cancel -> Cancelled   (×5)
-- app error does not poison the connection --
id=...00000000 APP-ERR BadArgs ... add needs two i64
id=...00000000 OK ... (utf8: still alive)       # 应用错误后连接继续可用
```

### 3.2 单请求

```
brpc echo 二进制RPC   -> OK，UTF-8 原样返回
brpc add 1000 -13     -> OK，i64: 987
brpc slow 120         -> OK，real 0m0.141s
brpc stats            -> OK，7×u64 统计快照
brpc sample-gen samples -> 生成 4 个 .bin + README
```

### 3.3 裸 TCP（python 原始 socket，独立于本项目客户端）

发送 `samples/*.bin`，各自正确往返：

```
request_echo.bin   req35B -> resp magic=b'BRPC' kind=2 id=0x100000001 plen=11 code=0
request_slow.bin   req35B -> resp ... id=0x100000002 plen=7  code=0
request_add.bin    req42B -> resp ... id=0x100000003 plen=10 code=0
request_stats.bin  req26B -> resp ... id=0x100000004 plen=58 code=0
```

现场异常注入（逐字节发送正常帧、一次 send 粘三帧、坏魔数、坏 CRC）：

```
half-packet echo: recv kind=2 id=0x65  code=0
sticky packet response ids: ['0xc9', '0xca', '0xcb']
bad magic: server returned b'' (len 0 => clean EOF/close)
fresh conn after error-frame: recv kind=2 id=0x191 code=0   # 新连接不受影响
bad crc:  server returned b'' (len 0 => close)
```

服务端最终 Stats：`requests=30, responses_ok=23, responses_app_err=1,
cancelled=5, framing_errors=2, unknown_cancel=0, rejected_inflight=0`
——两次成帧错误（坏 magic、坏 CRC）确实被计数并各自关闭了那条连接。

> netcat 说明：本机 `nc -q1` 对「服务端不主动关闭长连接」的场景不回显数据
> （它等待对端关闭），这是 nc 用法问题而非协议问题；故裸 TCP 验证统一用 python
> socket 完成。README/samples 中同时给出了 nc 与 python 两种发送方式。

---

## 4. 开发过程中发现并修复的真实缺陷（诚实记录）

1. **CRC 覆盖范围越界/错误（核心 bug）**：初版 `frame::encode` 用
   `hasher.update(&out[4..HEADER_LEN])`，但此刻 `out` 只有 20 字节（CRC 字段尚未
   写入），导致 `slice index out of range`，且语义上会把 CRC 字段自身也算进去。
   修复为 CRC 仅覆盖 `out[4..20]`（version..length）+ payload；同步修正
   `verify_crc` 与 `recompute_crc`。修复后新增/通过 `frame::crc_covers_*`、
   `decode::crc_flip_detected` 等测试，并以 python `zlib.crc32` 互通交叉确认。
2. **`HEADER_LEN` 循环定义**：最初在 `decode` 定义又被 `frame` 引用，造成私有重
   导入报错。改为在 `frame` 唯一定义，`decode` 引用。
3. **客户端生命周期/共享**：初版把连接线程句柄放进被多线程共享的 `MuxClient`
   （`Mutex<Option<JoinHandle>>`），无法 `Clone` 进多线程。重构为
   `Connection`（独占、负责关停）+ `MuxClient`（仅持 `Arc<Inner>`，廉价
   `Clone`），并修正关停时通过 `shutdown(Both)` 让读线程随 EOF 退出。
4. **两个测试断言自身有误（非库缺陷）**：
   - ID 测试误假设空闲槽按 FIFO 弹出（实际是 LIFO 栈），已改为只断言真正的契约
     「同槽位代次 +1、完整 64 位 id 不复用」；
   - 「快速响应后再取消」原断言服务端 `unknown_cancel+1`，但客户端发现响应已到本地
     就**不再发 CANCEL 帧**（正确行为），故该计数不应增加；改为断言返回原响应，并
     另用裸 socket 的 `wire::cancel_for_unknown_id_*` 专门覆盖服务端「未知 id
     CANCEL」路径。

---

## 5. 未通过项 / 已知限制

- 验收测试**全部通过，无遗留失败**。
- 设计上刻意不做（非缺陷，README「非目标」已声明）：TLS、认证、分片传输
  （flags 已预留位）、断线自动重连；应用层 payload 为极简演示格式。
- 时序相关用例（取消竞争、迟到响应）使用真实 TCP + 有界重试轮询
  （`wait_until`），连续 3 次全量运行未观察到 flaky；极端过载机器上超时窗口可能
  需要调宽。
