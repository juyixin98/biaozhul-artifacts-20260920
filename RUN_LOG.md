# 运行记录（RUN_LOG）

本文件如实记录开发与验收过程中实际执行的命令、结果，以及中途出现并修复的问题。
环境：Linux 6.8.0、cargo/rustc 1.98.1、Python 3.12.3；日期 2026-09-23。

## 1. 最终结果（最近一次运行）

### `cargo test`

```text
running 16 tests          # src 内单元测试（frame/sync）
test result: ok. 16 passed; 0 failed
running 27 tests          # tests/end_to_end.rs 集成测试
test result: ok. 27 passed; 0 failed; 0 ignored; finished in 3.30s
doc-tests: 0 passed
```

合计 **43 个测试全部通过，0 失败**（16 单元 + 27 集成）。
连续两轮 `cargo test` 结果一致，未发现时序性 flake。

### `cargo clippy --all-targets`

```text
（无 warning / error 输出，零警告）
```

### 一键验收 `./samples/scripts/run_acceptance.sh`

```text
ACCEPTANCE RESULT: PASS (all steps succeeded)
$ cat test-results/status.txt
PASS
```

脚本覆盖：debug/release 构建、全部测试、Rust demo-client 四个场景、
Python 互操作单请求、100 连放粘包+逐字节半包读取、9 种故障注入。

## 2. demo-server / demo-client 实际输出

服务端：

```text
$ ./target/release/demo-server --bind 127.0.0.1:9099
brpc-reuse demo server listening on 127.0.0.1:9099
  max_payload   = 1048576 bytes
  max_inflight  = 64
press Ctrl-C to stop
```

客户端（`all`）实测输出：

```text
connected to 127.0.0.1:9099

== basic: echo / upper / add / fail ==
ECHO  -> OK "hello rpc"
UPPER -> OK "MIXED CASE 123"
ADD   -> OK "\0\0\0\0\0\0\0*"          # 42 的大端 u64
FAIL  -> APP_ERROR service forced failure

== concurrent: 10 SLOW requests, responses arrive in id order ==
  task 0 .. task 9 全部 OK（后发请求延迟更短、先返回，仍按 id 正确路由）

== late response: timeout a SLOW request, then watch the answer arrive ==
request 15: timed out as expected (13)       # 13 = Timeout
late responses recorded: 1
  id=15 RESPONSE (no outstanding request with this id (timeout/cancel/never issued))

== cancel race: CANCEL a long SLOW request ==
request 16: cancelled by server (RpcError { code: Cancelled, message: "request cancelled" }, 12)
late/duplicate frames after cancel: []
exit=0
```

## 3. Python 独立实现互操作（跨语言校验帧格式与 CRC）

```text
$ python3 samples/scripts/frame_tool.py send ... echo/upper/add/fail
reply id=101: RESPONSE OK text='cross-language check'
reply id=102: RESPONSE OK text='MIXED CASE 42'
reply id=103: RESPONSE OK u64=1000023
reply id=104: RESPONSE APP_ERROR 'service forced failure'
（--half-reads 逐字节读取回复同样成功）

$ python3 samples/scripts/frame_tool.py soak 127.0.0.1:9099 --n 100
soak OK: 100/100 sticky-sent requests, byte-at-a-time replies; all matched by id (arrival OUT OF ORDER — routed correctly)
# 另一次运行观察到 arrival in id order —— 到达顺序本就不确定，按 id 匹配才是正确做法
```

故障注入实测（与 §错误处理设计逐条一致）：

```text
bad-crc        -> server CLOSED the connection (fatal framing error) — expected
bad-version    -> server CLOSED ... unsupported protocol version 9 — expected
oversized      -> server CLOSED ... payload too large: declared 16777216, limit 1048576
unknown-cmd    -> ERROR code=5 (UnknownCommand)，随后 PING->PONG，连接存活
bad-flags      -> ERROR code=4 (UnknownFlag)，随后 PING->PONG，连接存活
garbage        -> server CLOSED（垃圾字节被判为坏 magic）
half-frame     -> 截断帧后关本端；服务端记录 "peer closed mid-frame" 并清理
dup-id         -> ERROR code=7 (DuplicateRequest)，随后 PING->PONG
cancel-unknown -> ERROR code=9 (NoSuchRequest)，随后 PING->PONG
```

## 4. 验收清单逐条对照

| 要求 | 状态 | 证据 |
|---|---|---|
| 增量字节解析库（不用现成解析器） | ✅ | `src/frame.rs`，零依赖；逐字节/任意切点/粘包测试 |
| 明确支持子集 | ✅ | README §2.1 表格列出支持与不支持项 |
| 长度上限 | ✅ | 头部声明即拒绝（不按声明长度分配）；client/server 可配 |
| 明确错误类型 | ✅ | `src/error.rs`：16 个传输码 + 应用码；致命/可恢复分类 |
| version/请求ID/长度/CRC 帧 | ✅ | 13 字节头，CRC-32 用标准向量 `123456789 -> 0xCBF43926` 校验 |
| 同连接并发 | ✅ | 20 路乱序集成测试、500 路单连接压力测试 |
| 取消与响应乱序 | ✅ | 取消胜/负两个竞争测试；乱序按 id 路由 |
| 请求 ID 未释放前不得复用 | ✅ | 单调 u32，绝不重用；重复在途 id 被 DuplicateRequest 拒绝 |
| 迟到响应可识别 | ✅ | 超时后服务端迟到 RESPONSE 进入 `take_late()`，测试断言 id |
| 模拟半包/粘包 | ✅ | 单元测试、集成测试、Python soak（100 帧单 write + 逐字节读） |
| 超时后迟到响应 | ✅ | 专项测试 + demo `late` 场景实测 |
| 取消竞争 | ✅ | 双向竞争测试 + demo `cancel` 场景实测 |
| 内存有界 | ✅ | 1 GiB 虚假声明测试断言缓冲 < 64 KiB；双端在途信号量；有界写队列；有界迟到历史 |
| 错误帧后的连接处理 | ✅ | 可恢复错误后 PING 成功；致命错误关连接且解析器中毒 |
| 源码/README/请求样例/自动化测试 | ✅ | 全部交付；样例含原始 bin、标注 hex |
| 实际运行并如实记录 | ✅ | 本文件 + `test-results/acceptance.log` |
| 不做前端 | ✅ | 无任何前端代码 |

## 5. 开发过程中出现过的问题及修复（如实记录）

1. **初版解析器语义混淆**：第一版把“未知 command/flag”也当作解析错误处理，
   还引入了不存在的枚举变体导致编译失败。重构为“解析器只校验分帧
   （magic/version/length/CRC），command/flag 作为帧内容交给协议层”，
   未知 command 以 `Command::Unknown(byte)` 原样保留。
2. **`fail` 帧语义测试假设错误**：集成测试最初断言应用级 Fail 返回传输
   ERROR；按设计它是 RESPONSE 内的应用状态码 1。修正测试与 demo 客户端后通过。
3. **`bad_magic` 单元测试只喂 5 字节**：头部未满 13 字节时正确行为是
   `NeedMore` 而非 `BadMagic`；补全为 13 字节后通过。
4. **自实现有界通道 API 触发 clippy `result_unit_err`**：把
   `recv_timeout -> Result<Option<T>, ()>` 改为显式枚举
   `RecvTimeout::{Item,Closed,TimedOut}`，调用点同步更新。
5. **验收脚本路径 bug（首轮 FAIL，已如实保留现象）**：
   - 脚本位于 `samples/scripts/`，却 `cd` 到上一级，导致
     `target/release/demo-client: No such file or directory` 和
     `samples/samples/scripts/frame_tool.py` 双重路径；
   - 修正基准目录为脚本目录的上两级后，重新运行结果为 **PASS**。
6. **500 路压力测试首轮 FAIL（`TooManyInflight`）**：500 个线程同时 `call()`
   撞上 128 的在途上限而直接失败——这是有界设计的正确行为，是测试写法错误。
   测试改为遇到 `TooManyInflight` 时退避重试，验证槽位随响应归还，之后稳定通过。
7. **Python soak 首轮偶发 FAIL（`assert rid == 1000 + i`）**：soak 错误地假设
   echo 响应按 id 顺序到达；服务端每请求一个 worker 线程，顺序本就不保证
   （多次运行实测有时按序、有时乱序）。改为按 request_id 收集并校验每个 id
   恰好返回一次、载荷正确——这正是复用/乱序语义的独立验证。
8. **soak 与默认在途上限（64）的潜在竞争**：一次发 100 个请求时，理论上可能
   收到 ServerBusy（上限本身有专门测试覆盖，不应让 soak 承担）。验收脚本
   改为以 `--max-inflight 256` 启动服务端。
9. 其余若干 Rust 编译期问题（类型标注、Debug trait、`&[u8]`/`Vec<u8>`
   不匹配、match 臂语法）均在开发中即时修复，最终 `cargo build`、
   `cargo test`、`cargo clippy`、`cargo fmt --check` 全部干净通过。

## 7. 最终验收稳定性

修复问题 6–8 后，完整验收脚本连续运行 **3 次均 PASS**：

```text
run 1: exit=0 PASS
run 2: exit=0 PASS
run 3: exit=0 PASS
soak OK: 100/100 ... (arrival OUT OF ORDER — routed correctly)
soak OK: 100/100 ... (arrival in id order)
soak OK: 100/100 ... (arrival OUT OF ORDER — routed correctly)
```

## 6. 复现命令汇总

```bash
cargo build --release
cargo test
cargo clippy --all-targets
./samples/scripts/run_acceptance.sh          # 一键验收，退出码 0

# 手动
./target/release/demo-server --bind 127.0.0.1:9000
./target/release/demo-client --addr 127.0.0.1:9000 all
python3 samples/scripts/frame_tool.py soak 127.0.0.1:9000 --n 100
python3 samples/scripts/frame_tool.py inject 127.0.0.1:9000 bad-crc
```

未通过项：**最终状态无未通过项**。所有已知边界（明文、无重连、阻塞线程模型、
CANCEL 尽力语义）已在 README §8“已知限制”中说明，这些属于设计范围声明而非缺陷。
