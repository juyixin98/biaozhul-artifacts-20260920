# brpc-reuse — 二进制 RPC 复用（纯后端，纯 Rust 标准库实现）

在同一条 TCP 连接上复用多个并发 RPC 的纯后端项目。核心部分**全部从零实现**：
增量字节解析器、CRC-32、帧编解码、请求复用表、超时/取消/迟到响应处理；
**没有使用任何现成的协议解析器或第三方 crate**（`Cargo.toml` 依赖列表为空）。

- 语言：Rust 2021 edition（开发环境 rustc/cargo 1.98.1）
- 依赖：**零第三方依赖**，仅用 `std`
- 前端：无（本项目不包含也不需要前端）

---

## 1. 目录结构

```
a/
├── Cargo.toml
├── README.md                     # 本文档
├── RUN_LOG.md                    # 实际运行命令与结果的如实记录
├── src/
│   ├── lib.rs                    # 模块入口与公共导出
│   ├── frame.rs                  # 帧格式 / CRC-32 / 编码器 / 增量解析器（核心）
│   ├── error.rs                  # 明确的错误类型体系（致命 vs 可恢复）
│   ├── sync.rs                   # 自实现的有界 MPSC / 信号量 / 取消令牌
│   ├── client.rs                 # 复用客户端：并发、乱序、超时、取消、迟到识别
│   ├── server.rs                 # 复用 TCP 服务端：有界并发、取消竞争处理
│   ├── service.rs                # 本地测试用的微型应用协议（echo/upper/slow/add/fail）
│   └── bin/
│       ├── server.rs             # demo-server 可执行文件
│       └── client.rs             # demo-client 可执行文件
├── tests/
│   └── end_to_end.rs             # 27 个集成测试（真实 loopback TCP）
└── samples/
    ├── binary/                   # 请求样例（原始帧字节，含粘包/半包/坏 CRC）
    ├── hex/                      # 带字段标注的十六进制样例
    └── scripts/
        ├── frame_tool.py         # 纯 Python 标准库的独立帧工具（互操作/注入）
        └── run_acceptance.sh     # 一键自动化验收（构建+测试+运行+注入）
```

---

## 2. 线路协议（帧格式）

所有多字节整数均为**大端序**。

```text
偏移  长度  字段
0     2    magic      = 0x42 0x51（ASCII "BQ"）
2     1    version    = 0x01（唯一支持的版本）
3     1    flags      bit0 = REQ_ACK；其余保留位必须为 0（服务端按帧拒绝）
4     1    command    REQUEST=1 RESPONSE=2 CANCEL=3 ERROR=4 PING=5 PONG=6
5     4    request_id
9     4    payload_len
13    N    payload（N = payload_len，受长度上限约束）
13+N  4    crc32      对 [0 .. 13+N) 计算 CRC-32/ISO-HDLC（与 zlib/gzip 相同）
```

- 固定头 13 字节，CRC 尾 4 字节。空载荷帧总长 17 字节。
- CRC 采用 IEEE 802.3 反射多项式 `0xEDB88320`，init/xorout 均为 `0xFFFFFFFF`，
  查找表在编译期由 `const fn` 生成。
- 默认单帧载荷上限 `1 MiB`（可在 client/server 配置中覆盖）。

### 2.1 支持子集（明确边界）

| 能力 | 是否支持 | 说明 |
|---|---|---|
| REQUEST / RESPONSE / ERROR | ✅ | 单向请求-响应模型 |
| CANCEL | ✅ | 尽力通知，不保证服务端来得及停下 |
| PING / PONG | ✅ | 保活/连通性探测，占用在途名额 |
| 同连接多请求并发 | ✅ | 由 request_id 复用 |
| 响应乱序到达 | ✅ | 按 id 路由到各自的等待者 |
| 长度上限 | ✅ | 声明长度超限**在读取载荷前**即拒绝 |
| 请求取消 | ✅ | 单终帧语义：响应与取消竞争，先到者赢且只交付一次 |
| 请求 id 不复用 | ✅ | 单调递增；释放后也**绝不**重用 |
| 迟到响应识别 | ✅ | id 已无在途请求 ⇒ 记入迟到日志（有界） |
| 流式/分块响应 | ❌ | 一请求一终帧 |
| 协商/握手/鉴权/加密 | ❌ | 明文字节协议，仅本地测试用途 |
| 重连/重传 | ❌ | 致命分帧错误直接关连接（见 §5） |

### 2.2 ERROR 帧载荷格式

```text
code:u16-be   message:utf8...
```

错误码定义见 `src/error.rs`，其中 1–16 为协议/传输层，64 为应用层。

---

## 3. 核心设计：增量字节解析器（`frame::IncrementalDecoder`）

面向 TCP 字节流，不依赖任何现成解析器：

- `feed(&[u8])`：喂入任意长度的 TCP 读取结果（一次一个字节或一次几万字节均可）。
- `next_frame()`：取出一个完整帧；数据不足返回 `ParseError::NeedMore`
  （**这不是错误**，继续喂即可）。
- 天然覆盖两种 TCP 现象：
  - **半包**：一个帧被切成多次 read，解析器持有不完整缓冲直到凑齐。
  - **粘包**：一次 read 含多个帧，循环 `next_frame()` 逐个取出。
- 缓冲随消费压缩（`pos` 越过容量一半且超过 4 KiB 时 drain），不会随帧数增长。

### 3.1 内存有界性论证

单连接上的内存上界由以下几部分构成，均为配置常量：

1. **解析缓冲**：一旦头部可见且 `payload_len > max_payload`，立即致命拒绝，
   **不会**按声明长度分配内存；持有字节数 ≤ 一个 read chunk
   （代码中 16 KiB）+ `max_payload + 17`。
2. **在途请求表**：客户端 `max_inflight`（默认 64）个槽位，由自实现的计数
   信号量限制；满了直接返回 `TooManyInflight`，不入表、不排队。
3. **服务端在途工作**：同样由 `max_inflight` 个许可限制；满了返回
   ERROR(`ServerBusy`)，不会无界 spawn 线程。
4. **写队列**：有界 MPSC（容量 `writer_queue_capacity`，默认 128），满时写者
   阻塞形成背压，进而使在途信号量饱和、新请求收到 ServerBusy。
5. **迟到响应历史**：固定保留最近 256 条，超出丢弃最旧并累加丢弃计数。
6. **请求 id 空间**：单调 `u32`，耗尽（`u32::MAX`）直接报错而不是回绕复用。

---

## 4. 并发复用、取消与乱序（客户端语义）

- 每个请求占一个槽位并获得单调 id；reader 线程按 id 把终帧投入对应单槽通道。
- **第一个终帧完成调用，随后任何同 id 帧都被识别为迟到**（路由前先摘除槽位）。
- `wait_timeout`：超时后立即本地摘除槽位并尽力发送 CANCEL；服务端事后完成的
  RESPONSE 到达时已无在途表项 ⇒ 记入 `take_late()`，不会张冠李戴。
- `cancel`：先发 CANCEL 再继续等待——服务端可能已经做完，此时真实 RESPONSE
  照常赢下竞争；否则收到 ERROR(`Cancelled`)。
- 服务端 worker 在发送终帧前先从在途表摘除自己，使“响应 vs 取消”成为一次
  简单的布尔检查，保证**每请求恰好一个终帧**。

---

## 5. 错误类型与错误帧后的连接处理

错误被明确分为两类（`ErrorCode::is_fatal`）：

**致命分帧错误 —— 关闭连接**（流的边界信任已被破坏，继续解析不安全）：

| 情形 | 错误 | 行为 |
|---|---|---|
| magic 错误 / 非本协议字节流 | CrcMismatch 类（本地记录 BadMagic） | 解析器中毒，关连接 |
| version ≠ 1 | UnsupportedVersion | 关连接 |
| payload_len 超上限 | PayloadTooLarge | 读到头部即关连接 |
| CRC 校验失败 | CrcMismatch | 解析器中毒，关连接 |
| 流在帧中途结束（干净 EOF） | UnexpectedEof | 关连接 |

解析器中毒后拒绝再喂数据（`Poisoned`），杜绝“跳过坏帧继续读”造成的错位。

**可恢复的按帧协议错误 —— 回 ERROR 帧，连接继续**：

| 情形 | 错误码 |
|---|---|
| 未知 command 字节 | UnknownCommand(5) |
| 保留 flag 位置位 | UnknownFlag(4) |
| 同 id 请求已在途 | DuplicateRequest(7) |
| 取消一个不在途的 id | NoSuchRequest(9) |
| 帧方向错误（如服务端收到 PONG） | InvalidDirection(10) |
| 服务端在途已满 | ServerBusy(8) |
| 应用载荷无法解码 | AppError(64) |

本地状态错误（不上线）：`Cancelled(12)`、`Timeout(13)`、`ConnectionClosed(14)`、
`TooManyInflight(15)`、`Poisoned(16)`、`Shutdown(11)`。

---

## 6. 构建、测试、运行

### 6.1 构建

```bash
cargo build            # debug
cargo build --release  # release
```

零网络依赖，无需联网下载 crate。

### 6.2 自动化测试

```bash
cargo test             # 16 个单元测试 + 27 个集成测试
cargo clippy --all-targets   # 静态检查（开发过程中保持零警告）
```

### 6.3 手动运行 demo

```bash
# 终端 1
./target/release/demo-server --bind 127.0.0.1:9000

# 终端 2
./target/release/demo-client --addr 127.0.0.1:9000 all
# 也可单独运行：basic | concurrent | late | cancel
```

### 6.4 一键自动化验收

```bash
./samples/scripts/run_acceptance.sh
echo $?                       # 0 = 全部通过
cat test-results/status.txt   # PASS / FAIL
# 完整日志：test-results/acceptance.log
```

该脚本依次：构建 debug/release → `cargo test` → 启动服务端（临时端口）→
运行 Rust demo-client 全部场景 → 用独立 Python 实现做单请求、
**粘包发送 + 逐字节读取（半包）** 100 连放、以及 9 种故障注入。

### 6.5 独立 Python 帧工具（只用标准库 struct/socket/zlib）

```bash
# 打印带字段标注的请求帧
python3 samples/scripts/frame_tool.py hexdump echo --id 1 --text 'hello rpc'
python3 samples/scripts/frame_tool.py hexdump add  --id 2 --num 40 2
python3 samples/scripts/frame_tool.py hexdump slow --id 3 --delay 1000 --text late-body

# 向运行中的服务端发帧并解码回复（--half-reads 逐字节读）
python3 samples/scripts/frame_tool.py send 127.0.0.1:9000 upper --id 7 --text Hello

# 粘包+半包压力（100 帧一次 write，回复逐字节 read）
python3 samples/scripts/frame_tool.py soak 127.0.0.1:9000 --n 100

# 故障注入：bad-crc bad-version oversized unknown-cmd bad-flags
#           garbage half-frame dup-id cancel-unknown
python3 samples/scripts/frame_tool.py inject 127.0.0.1:9000 bad-crc

# 解析样例文件（可含多帧粘包）
python3 samples/scripts/frame_tool.py dump samples/binary/10-sticky-three-requests.bin
```

---

## 7. 验收点与对应测试

| 验收要求 | 覆盖方式 |
|---|---|
| 半包 | 单元测试按字节喂入；集成测试任意切点；Python `--half-reads` 逐字节读 |
| 粘包 | 50/100 帧同一缓冲；`10-sticky-three-requests.bin`；Python 单次 write 100 帧 |
| 同连接并发 | `concurrent_out_of_order_responses`（20 请求、后发先至）、500 请求压力测试 |
| 响应乱序 | 同上，按各自 id 正确路由 |
| 超时后迟到响应 | `timeout_then_late_response_is_recognised`、交错超时压力测试、demo `late` |
| 取消竞争 | `cancel_long_request_wins`、`cancel_that_loses_race_returns_real_response`、demo `cancel` |
| 请求 id 未释放前不得复用 | 单调分配测试 + `duplicate_inflight_id_rejected_by_server` |
| 迟到响应可识别 | `take_late()` 记录 request_id/命令/原因，测试与 demo 均断言 |
| 内存有界 | 1 GiB 虚假声明不分配测试、客户端/服务端在途上限测试、ServerBusy 测试 |
| 错误帧后连接处理 | 可恢复错误后 PING 仍成功；坏 CRC/版本/垃圾/半包 EOF 关连接 |

完整运行记录见 [RUN_LOG.md](RUN_LOG.md)。

## 8. 已知限制

- 明文协议，无 TLS/鉴权，仅用于本地测试与教学验证。
- 阻塞式线程模型（每请求一线程），适合验证语义；生产级高并发应换 async 运行时。
- CANCEL 是尽力语义；服务端在不可中断的计算段中无法立即停下。
- 无自动重连：连接死亡后调用返回 `ConnectionClosed`，需由上层新建连接。
