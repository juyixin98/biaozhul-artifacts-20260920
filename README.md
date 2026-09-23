# wsframe — 手写 WebSocket 帧重组库与本地 TCP 测试服务

纯后端项目，**从零手写** RFC 6455（WebSocket）服务端在握手完成后的**增量字节帧解析
与消息重组**，外加一个用于本地验证的 TCP 回声服务 `wsecho`。

- 语言：Rust 2021 edition，**零第三方依赖**（`Cargo.toml` 无 `[dependencies]`）。
- 没有使用任何现成的 WebSocket 协议解析器：帧解析、掩码处理、分片状态机、
  跨分片 UTF-8 校验、握手所需的 SHA-1/Base64 全部自行实现。
- 无前端、无 UI；只有库、一个命令行服务、自动化测试与探针脚本。

---

## 1. 支持的协议子集与约束

### 支持

| 项目 | 情况 |
| --- | --- |
| 角色 | **服务端**视角：接收客户端（必须掩码）的帧，发送不掩码的帧 |
| 数据帧 | text(0x1)、binary(0x2)、continuation(0x0)，支持任意分片数 |
| 控制帧 | close(0x8)、ping(0x9)、pong(0xA) |
| 帧长度 | 7 位、16 位、64 位三种长度字段；掩码去除 |
| Ping | 立即回 Pong 并原样带回应用数据 |
| 分片 | 严格校验起始/续帧序列；**控制帧可插入数据分片中间**，不影响重组 |
| 文本 UTF-8 | **跨分片增量校验**，允许半个多字节字符落在帧边界；拒绝超长编码、代理区、越界码点 |
| 关闭握手 | 解析/回显状态码与原因；收到 Close 回 Close 后断开 |
| 打开握手 | HTTP/1.1 `Upgrade`，校验 method/Upgrade/Connection/Key(16B base64)/Version=13，计算 `Sec-WebSocket-Accept`；非法握手回 HTTP 400 |

### 明确**不**支持（遇到即按协议错误处理）

- 扩展与子协议协商：因此 **RSV1/RSV2/RSV3 必须为 0**，否则 1002。
- 保留操作码 0x3–0x7、0xB–0xF → 1002。
- 客户端→服务端未掩码帧 → 1002；分片的控制帧、控制帧载荷 >125 字节 → 1002。
- 保留关闭码（如 1004/1005/1006）出现在线上 Close 帧中 → 1002。
- 不实现 permessage-deflate、不做 TLS（本地明文测试即可）、不实现客户端角色。

### 长度上限（可配置）

| 限制 | 默认值 | 超限结果 |
| --- | --- | --- |
| 单帧 payload `--max-frame-bytes` | 1 MiB | **1009**（帧头层即拒绝） |
| 整条重组消息 `--max-message-bytes` | 64 KiB | **1009**（追加后超限时） |
| 控制帧 payload | RFC 硬上限 125 字节 | **1002** |

---

## 2. 错误类型与关闭码映射

`src/error.rs` 的 `WsError` 与 `WsError::close_code()`：

| 错误 | 关闭码 | 含义 |
| --- | --- | --- |
| `FrameNotMasked` | 1002 | 客户端帧未掩码 |
| `RsvBitsSet` | 1002 | RSV 非零 |
| `UnknownOpcode` | 1002 | 保留操作码 |
| `ControlFrameTooLong` | 1002 | 控制帧 >125B |
| `ControlFrameFragmented` | 1002 | 控制帧 FIN=0 |
| `IllegalFragmentation` | 1002 | 非法连续帧序列 |
| `InvalidCloseFrame` | 1002 | 关闭码非法/半字节/原因非 UTF-8 |
| `ConnectionClosed` | 1002 | Close 之后又来帧 |
| `InvalidUtf8` | **1007** | 文本（跨分片）不是合法 UTF-8 |
| `FrameTooLarge` / `MessageTooLarge` | **1009** | 帧/消息超长 |
| `Incomplete` | （不关闭） | 字节还不够，继续喂即可 |

---

## 3. 目录结构

```
Cargo.toml
src/
  lib.rs          模块汇总与公开 API
  error.rs        WsError、关闭码、合法性判定
  frame.rs        帧头解析、去掩码、Close 载荷解析、服务端帧编码
  utf8.rs         可跨分片续写的增量 UTF-8 状态机
  reassemble.rs   核心：逐字节喂入的帧/消息重组状态机
  sha1.rs         手写 SHA-1 + Base64（仅握手用）
  handshake.rs    HTTP 升级请求解析与 Accept 计算
  server.rs       本地 TCP 回声服务
  main.rs         wsecho 命令行
tests/            真实 TCP 端到端集成测试（随机端口、逐字节发送）
scripts/
  ws_probe.py     仅用标准库的探针客户端（请求样例/验收脚本）
samples/
  handshake_request.txt   打开握手请求样例
  client_frames.hex       客户端帧十六进制逐字节样例（含注释）
run_tests.sh      一键测试（cargo test；--probe 额外做端到端探针）
```

---

## 4. 快速开始

需要 Rust（开发环境为 rustc/cargo 1.98）与 Python 3（仅探针脚本，标准库即可）。

```bash
# 编译
cargo build --release

# 方式 A：标准 HTTP 握手模式
./target/release/wsecho --addr 127.0.0.1:9001

# 方式 B：raw 模式（跳过握手，TCP 连上即按帧处理，便于裸字节测试）
./target/release/wsecho --addr 127.0.0.1:9001 --raw

# 自定义长度上限（支持 k/m/g 后缀）
./target/release/wsecho --raw --max-message-bytes 1k --max-frame-bytes 64k
```

命令行参数：`--addr`、`--raw`、`--max-frame-bytes`、`--max-message-bytes`、
`--quiet`、`-h/--help`。

### 用探针客户端跑验收场景

```bash
# 服务端（一个终端）
./target/release/wsecho --raw

# 另一个终端：逐字节发送各场景，打印服务端响应与关闭码
python3 scripts/ws_probe.py --raw --case ping-insert --byte-by-byte
python3 scripts/ws_probe.py --raw --case half-utf8-valid --byte-by-byte
python3 scripts/ws_probe.py --raw --case too-big
python3 scripts/ws_probe.py --raw --case reserved-close-code
# 全部场景：
for c in echo ping-insert illegal-cont double-start half-utf8-valid \
         half-utf8-truncated bad-utf8-byte too-big control-too-long \
         reserved-opcode unmasked close-codes reserved-close-code; do
  python3 scripts/ws_probe.py --raw --case "$c" --byte-by-byte
done
```

握手模式（脚本会完成真实 HTTP 升级并校验 `Sec-WebSocket-Accept`）：

```bash
./target/release/wsecho --addr 127.0.0.1:9001
python3 scripts/ws_probe.py --port 9001 --case echo --byte-by-byte
```

### 一键测试

```bash
./run_tests.sh             # 仅 cargo test（单测 + 集成测试）
./run_tests.sh --probe     # 额外构建 release、起真实服务、跑 13 个探针场景
```

---

## 5. 核心 API（库用法）

```rust
use wsframe::{Reassembler, Event};
use wsframe::frame::Limits;

let mut r = Reassembler::new(Limits::DEFAULT);

// 真正的“逐字节”输入；Ok(None) 表示字节收下了但帧/消息尚未完整
for &b in stream_bytes {
    match r.feed(b) {
        Ok(Some(Event::Text(s)))     => { /* 完整文本消息 */ }
        Ok(Some(Event::Binary(v)))   => { /* 完整二进制消息 */ }
        Ok(Some(Event::Ping(p)))     => { /* 回 Pong，带回 p */ }
        Ok(Some(Event::Close{code,..})) => { /* 回 Close 后断开 */ }
        Ok(None) => {}                       // 还需更多字节
        Err(e) => {
            let (code, reason) = e.close_code().unwrap(); // 致命协议错误
            // 发送 code 关闭帧并关闭连接
        }
    }
}
```

设计要点：

- `Reassembler::feed` 一次一个字节，内部先在帧层用 `parse_header`
  从缓冲区按需解析帧头（2/4/10 字节 + 4 字节掩码密钥），收满 payload 后去掩码；
  帧头/载荷跨任意字节边界到达都能从断点继续。
- 分片状态用 `Idle / Text(增量UTF8) / Binary` 表达。
  - 有未结束分片却又来 text/binary 起始帧，或无起始帧就来 continuation →
    `IllegalFragmentation`(1002)。
  - Ping/Pong/Close 在任意时刻（含分片进行中）处理且**不改分片状态**。
- 文本消息在起始分片上创建 `IncrementalUtf8`，每个续帧续写；
  起始/中间分片允许结尾停在半个字符，仅在 fin 时要求停在字符边界，否则 1007。

---

## 6. 自动化测试

- **库内单元测试 40 个**：帧头解析/掩码、各种长度字段、控制帧约束、保留码、
  SHA-1/Base64 已知向量、增量 UTF-8（半字符、代理、超长、跨块）、重组状态机全部分支。
- **真实 TCP 集成测试 25 个**（`tests/`，每个用例随机端口起服务）：
  - `echo_and_ping.rs`：回声、**Ping 插入分片**、未请求 Pong 忽略；
  - `illegal_frames.rs`：非法连续帧、重复起始、分片控制帧、保留 opcode、RSV、未掩码；
  - `utf8_cross_fragments.rs`：半个/四字节字符跨帧（夹 Ping）、截断与非法字节、代理/超长；
  - `limits_and_close.rs`：分片/单帧/默认上限的 **1009**、控制帧 126B 的 **1002**、
    1000/3000 回显、保留码 1005/半字节关闭码的 **1002**；
  - `incremental_byte_feed.rs`：**逐字节**发送、半帧头/半载荷静默、扩展长度字段拆分；
  - `handshake_e2e.rs`：真实 101、RFC Accept 值、缺 key 返回 400、逐字节握手请求。
- **端到端探针** `scripts/ws_probe.py`：13 个场景，真实起服务运行，输出见
  `docs/test_run.md`（含命令与原始结果）。

---

## 7. RFC 依据（实现时对照的关键条款）

- §5.1 / §5.2：帧格式、掩码方向；§5.3：掩码算法；
- §5.4：分片规则（起始帧/续帧、控制帧可穿插）；
- §5.5：控制帧（Close/Ping/Pong）长度与处理、关闭握手；
- §5.6：文本消息按重组后的完整数据做 UTF-8 校验；
- §7.4：状态码（1000/1002/1007/1009 等）与保留码；
- §4.1/§4.2：打开握手与 `Sec-WebSocket-Accept = base64(sha1(key + GUID))`。
