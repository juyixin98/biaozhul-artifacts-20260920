# TLS 记录观察器（TLS Record Observer）

纯后端、零三方依赖的 Rust 项目。对 **TLS 1.0–1.3 客户端方向字节流**做**增量**
观察：只解析 **TLS 记录头（record header）**和**明文 `ClientHello`**，把握手消息
**跨记录重组**，输出 **SNI、ALPN 与未知扩展**。**不解密、不实现 TLS 握手、不参与
密钥交换**，也不做任何前端。

> 这是一个“观察器”（observer），不是协议栈：它被动读取 client→server 方向的原始
> TCP 字节，给出结构化观察结果或明确错误。

---

## 1. 目录结构

```
.
├── Cargo.toml
├── README.md
├── src/
│   ├── lib.rs           # 库入口、公开 API、hex 工具
│   ├── cursor.rs        # 手写增量字节游标（Reader：u8/u16/u24、定长切片）
│   ├── error.rs         # 显式错误类型 ParseError 与非致命 Warning
│   ├── record.rs        # TLS 记录头解析（RFC 8446 §5.1，仅 5 字节头）
│   ├── client_hello.rs  # 明文 ClientHello、扩展、SNI/ALPN、GREASE 判定
│   ├── observer.rs      # 记录状态机 + 跨记录握手重组（核心）
│   └── main.rs          # 本地 TCP 测试服务（内置 JSON 序列化）
├── tests/
│   └── observer.rs      # 26 个端到端合成报文测试
└── examples/
    ├── send_sample.py   # 纯标准库 Python：合成并发送各类报文
    ├── README.md        # 请求样例说明
    └── outputs/         # 实际运行保存的 JSON 输出样例
```

没有使用任何现成的完整 TLS 协议解析器；记录头、握手头、ClientHello、扩展的字节
解析全部在本仓库内手写（`cursor.rs` 是唯一的底层原语）。`Cargo.toml` 的
`[dependencies]` 为空，因此 **离线可构建**。

---

## 2. 支持的子集、长度上限与错误类型（明确边界）

### 2.1 支持的子集

| 层 | 接受 | 行为 |
|---|---|---|
| 记录层 `content_type` | `20` ChangeCipherSpec / `21` Alert / `22` Handshake / `23` ApplicationData | 其他值 → `BadContentType` |
| 记录层版本 `legacy_record_version` | 主版本固定 `3`，次版本 `1..=3`（SSL3.1=TLS1.0 … TLS1.3） | 其他 → `InvalidRecordVersion` |
| 握手类型 | 仅 `1` ClientHello 作为解析对象 | 其余类型（ServerHello 等）在完整成帧时**跳过并告警**，不猜测 |
| `ClientHello.client_version` | `(3,1)`、`(3,2)`、`(3,3)`、`(3,4)`（含 TLS1.3 的 legacy 0x0304 容差） | 其他 → `InvalidClientHelloVersion` |
| 扩展 | SNI(`0x0000`)、ALPN(`0x0010`) 解析；GREASE 分类；其余作为**不透明未知扩展**原样保留 hex | — |

明确**不做**：解密、证书、密钥交换、Finished、心跳、压缩、服务器方向语义、TLS 1.3
加密消息（`Handshake` type 在加密后也不解析）。

### 2.2 长度上限（`Limits`，可配置）

| 上限 | 默认值 | 超限错误 |
|---|---|---|
| `max_record_fragment`（记录分片） | `2^14 + 256 = 16640`（RFC 8446 硬上限） | `RecordTooLarge` |
| `max_handshake_message`（重组的握手消息） | `1 MiB`（远大于合法 ClientHello，防止恶意长度前缀导致无限缓冲） | `HandshakeTooLarge` |

字段级规则：`session_id ≤ 32`、`cipher_suites` 长度为**偶数且 ≥ 2**、
`compression_methods` 长度 **≥ 1**，违反分别报 `MalformedVector`。

### 2.3 错误类型（`ParseError`，全部显式，无“万能失败”）

- `Truncated { layer, where_ }`
  - `layer`：`RecordHeader / Record / HandshakeHeader / HandshakeBody / Extension`
  - `where_`：`Field`（定长字段不够）、`LengthPrefixed`（长度前缀超出）、
    `Reassembly`（流结束时握手消息还没拼完）
- `BadContentType(u8)`
- `InvalidRecordVersion { major, minor }`
- `RecordTooLarge { length, max }`
- `HandshakeTooLarge { length, max }`
- `UnsupportedHandshakeType(u8)`
- `InvalidClientHelloVersion { major, minor }`
- `LengthMismatch { layer, kind, what, declared, actual }`
  - `kind`：`Nested`（**内层长度与外层容器不符**）、`Consumed`、`Field`
- `MalformedVector { layer, what, length }`

非致命情况进入 `Warning`：`SkippedHandshake`、`DuplicateExtension`（重复扩展，**首个
生效**）、`GreaseExtension`、`GreaseCipherSuite`、`UnknownServerNameType`、
`InvalidSniUtf8`、`InvalidAlpnEntry`。

### 2.4 为什么密文不会被误解析成握手

1. 只有 `content_type == 22` 的**明文握手记录**分片才会送入重组器；
2. 重组器必须先看到结构合法的 **4 字节握手头**（类型 + u24 长度），且声明长度
   完整到齐后才解析 ClientHello，绝不靠“在字节里搜 `0x16`”猜测；
3. 一旦看到 **ApplicationData(23)** 或 **ChangeCipherSpec(20)**，立即关闭明文
   握手解析（置 `encrypted`）；
4. ClientHello 提取成功后同样关闭（客户端方向只有这一条明文握手消息）。

---

## 3. 增量解析与跨记录重组

- 输入 `Observer::push(&[u8])` 接受**任意 TCP 分块边界**：按字节喂入与一次性喂入
  结果一致（测试 `byte_by_byte_feeds_are_equivalent` 用 1 字节步长验证）。
- 记录头 5 字节与分片分开放心处理；未到齐时 `push` 返回 `Ok` 并保留 `carry`。
- 握手消息可任意跨记录（甚至 4 字节握手头本身被切开）：`hs_stream` 累积所有
  Handshake 分片，按 u24 长度切出完整消息，再交给 ClientHello 解析器。
- 流结束调用 `finish()`：若仍残留半条记录/半条握手消息，返回带定位的 `Truncated`，
  同时保留此前已观察到的内容（错误与部分观察结果一起返回）。

---

## 4. 构建与运行

需要 Rust（实测 `rustc 1.98.1`），无需联网拉取依赖。

```bash
cargo build --release

# 启动本地被动观察服务（默认 127.0.0.1:8443）
./target/release/tls-observer-server --port 8443

# 参数
#   --bind ADDR             默认 127.0.0.1
#   --port PORT             默认 8443
#   --idle-timeout-ms MS    读空闲超时，默认 2000
#   --max-bytes N           单连接字节硬上限，默认 4 MiB
```

服务端对每条连接：读取 client 字节直到对端**半关闭**（或空闲超时）→ 输出一条 JSON
观察结果（同时打印到 stdout 并回写给连接方）。它**不会**回应 TLS peer。

### 作为库使用

```rust
use tls_record_observer::Observer;

let mut o = Observer::new();
o.push(&part1)?;                 // 任意分块
o.push(&part2)?;
let observation = o.finish()?;   // 或 Err((ParseError, Observation))
if let Some(ch) = &observation.client_hello {
    println!("SNI={:?} ALPN={:?}", ch.sni, ch.alpn);
}
```

也有一次性便捷函数 `tls_record_observer::observe(bytes) -> (Result<(), ParseError>, Observation)`。

### 输出 JSON（字段）

`ok / bytes_in / encrypted / counts{...} / records[...] / client_hello{...} /
warnings[...] / error{type,layer,message}`。`client_hello` 内含
`client_version, random_hex, session_id_hex, cipher_suites, grease_cipher_suites,
compression_methods, sni, alpn, unknown_extensions[{type,data_len,data_hex}]`。

---

## 5. 请求样例

`examples/send_sample.py`（仅用 Python 标准库，实测 Python 3.12）手工合成记录层/
握手层字节，不做 TLS 握手。默认每 7 字节发一个 TCP 段以证明增量解析。

```bash
# 终端 A
./target/release/tls-observer-server --port 8443

# 终端 B：发送全部场景
python3 examples/send_sample.py all --port 8443

# 单个场景：valid | fragment | duplicate | nested-bad | grease |
#           trunc-record | trunc-hs | ciphertext | ciphertext-only |
#           bad-ctype | garbage
python3 examples/send_sample.py valid
```

也可以用**真实 TLS 客户端**驱动（服务被动，不回应握手，openssl 最终会超时报错，
这是预期；观察器仍能看到明文 ClientHello）：

```bash
openssl s_client -connect 127.0.0.1:8443 \
    -servername real.example.org -alpn h2,http/1.1 -tls1_2 < /dev/null
```

实际保存的输入/输出样例见 `examples/outputs/`，场景说明见 `examples/README.md`。

---

## 6. 自动化测试

```bash
cargo test
```

### 实际运行记录（本机，2026-09-23）

环境：`rustc 1.98.1`、`cargo 1.98.1`、`Python 3.12.3`、Linux 6.8。

```
running 2 tests          # src/lib.rs 单元测试（GREASE 判定、hex）
test result: ok. 2 passed; 0 failed
running 26 tests         # tests/observer.rs 端到端合成报文
test result: ok. 26 passed; 0 failed
```

**26 个集成测试全部通过，0 失败。**覆盖验收要求：

- 正常 ClientHello 提取 SNI/ALPN；按 1 字节喂入等价；
- **跨记录重组**：握手头跨 2 条记录、ClientHello 体跨 3 条记录且以 13 字节分块喂入；
- **重复扩展**：重复 SNI/ALPN → 告警且首个生效；
- **嵌套长度不符**：extensions 外层长度谎报、单扩展 data 越界、SNI 内层 list 长度不符；
- **GREASE**：`0x0a0a/…/0xfafa` 等被分类，不进入未知扩展/常规密码套件；
- **截断**：记录头截断、记录分片截断、跨记录握手消息流末未完成；
- **不把密文误解析为握手**：ClientHello 后的 ApplicationData、直接以 ApplicationData
  开头、ChangeCipherSpec 之后的伪握手、以 `0x16` 开头的随机垃圾；
- 子集/上限：未知 content_type、非法记录版本、记录超上限、握手 u24 超上限、
  非法 ClientHello 版本、奇数 cipher_suites 长度；
- Alert 记录计数、非 ClientHello 握手跳过告警、未知扩展不透明保留。

### 端到端服务实测

启动 release 服务后运行 `send_sample.py all`，**11/11 场景均返回合法 JSON**（用
Python `json.loads` 逐个校验）：6 个 `ok=true`，5 个按预期 `ok=false`
（`nested-bad / trunc-record / trunc-hs / bad-ctype / garbage`，分别对应
length_mismatch、truncated、bad_content_type）。

真实流量：用 OpenSSL 3.0 分别以 TLS 1.2 / TLS 1.3 发送 ClientHello，观察器正确给出
- TLS1.2：SNI `real.example.org`、ALPN `[h2, http/1.1]`、28 个密码套件、6 个未知扩展；
- TLS1.3：SNI `tls13.example.io`、ALPN `[h3]`、legacy `3.3`，扩展 `0x002b
  (supported_versions)`、`0x0033 (key_share)` 等作为未知扩展原样保留。

### 未通过项 / 已知限制（如实记录）

- **无未通过测试**：`cargo test` 28/28 通过，端到端 11/11 JSON 合法。
- 开发中修复过两个真实问题并复测通过：① 服务端在 fatal 解析错误时于客户端 FIN
  到达前写回，偶发 `Connection reset by peer`（改为排空到半关闭后再回写）；
  ② 早期 JSON 序列化在数组尾产生多余逗号导致非严格 JSON（已改为 join 生成，
  现全部通过 `json.loads` 严格解析）。
- 已知非目标/限制：只观察 client 单方向；不跟踪 TLS 1.3 的 middlebox-compat 加密
  握手记录语义；SNI 非 UTF-8 时丢弃该名字并告警而非报错；扩展只解析 SNI/ALPN，
  其余（含 supported_versions/key_share）有意作为不透明未知扩展输出；服务为单连接
  串行 accept，定位为本地测试工具而非生产服务器。
