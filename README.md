# TLS 记录观察器（tls-record-observer）

纯后端的 TLS **明文**观察工具：对一条 TCP 字节流做**增量**解析，只解析 TLS
**记录头**和明文 **ClientHello**，把跨记录分片的握手消息重组出来，输出
**SNI、ALPN、supported_versions 以及未知扩展**。

- 只用 Rust `std` 手写核心解析（`Cargo.toml` 无任何第三方依赖），**不使用**
  rustls / openssl / x509-parser 等现成协议解析器代做核心；
- **不解密、不实现 TLS 握手、不发送任何协议数据、不做前端**；
- 明确的支持**子集**、**长度上限**和**错误类型**；
- 关键安全性质：**绝不把密文 / 随机字节误解析为握手消息**（有模糊测试守护）。

适用场景：被动观测明文 ClientHello（SNI/ALPN 盘点）、教学、对合成畸形报文的
离线分析。它不是、也不打算成为 TLS 端点。

---

## 1. 目录结构

```
Cargo.toml                  无第三方依赖
src/
  lib.rs                    crate 入口与公共 API 导出
  error.rs                  ParseError：全部错误类型（硬错误 vs Truncated 软错误）
  config.rs                 Config：支持子集的长度上限
  reader.rs                 增量字节游标（越界=截断；越嵌套块=长度不符）
  grease.rs                 RFC 8701 GREASE 值识别（16 个值，穷举测试）
  record.rs                 TLSPlaintext 5 字节记录头解析
  client_hello.rs           明文 ClientHello 解析（SNI/ALPN/supported_versions/未知扩展）
  observer.rs               增量状态机 + 跨记录握手重组（核心）
  json.rs                   最小 JSON 转义（不引入 serde）
  server.rs                 本地 TCP 测试服务（每条连接一行 JSON 报告）
  test_support.rs           与解析器对称的报文构造器（含畸形长度入口）
  bin/server.rs             tls-observer-server 可执行文件
  bin/gen_samples.rs        gen-samples：生成合成报文样例
  bin/observe_file.rs       observe-file：离线文件观察器
tests/                      35 个集成测试（含真实 TCP 回环与密文模糊测试）
samples/                    16 个合成报文 .bin + MANIFEST.txt
scripts/                    demo.sh 端到端演示、feed_sample.py 发送脚本
examples/sample_output.jsonl  一次真实运行（含 openssl 真 ClientHello）的报告
RUNLOG.md                   实际命令、结果与发现/修复的问题（如实记录）
```

## 2. 支持子集（明确边界）

**记录层（RFC 8446 §5.1 的 TLSPlaintext 头）**
- ContentType 只接受 `20 change_cipher_spec / 21 alert / 22 handshake / 23 application_data`；
- 记录层版本只接受 `0x0301..=0x0304`（TLS 1.0–1.3 明文头所用的 legacy 版本）；
- 只解析 5 字节头；fragment 内容**仅**在明文握手阶段被解释。

**握手层**
- 只重组并检查第一条握手消息；只在其 `HandshakeType == 1 (ClientHello)` 时解析；
- 支持一条握手消息跨多条记录、切点落在握手头内部、一条记录含多条消息等情况；
- 第一条消息不是 ClientHello（如 ServerHello）即给出结论 `first_handshake_not_client_hello`。

**ClientHello（RFC 5246 §7.4.1.2 / RFC 8446 §4.1.2）**
- legacy_version 接受 `0x0301..=0x0304`；random(32)、session_id(≤32)、
  cipher_suites（非空、偶数长度、至少一个非 GREASE 套件）、compression_methods（非空）；
- 必须存在 extensions 块；
- 解析的扩展：`0 server_name(SNI)`、`16 ALPN`、`43 supported_versions`；
- 其余扩展一律登记为**未知扩展**（类型号 + data 长度 + 前 16 字节十六进制预览）；
- GREASE（RFC 8701）在扩展/套件/版本各槽位被识别并剔除，不计为未知、不参与去重。

**明确不做**：不解析 ServerHello 及之后任何消息，不解析 cookie / PSK 等更多扩展，
不做证书/密码学处理，不解密，不重放，不主动联网。

## 3. 长度上限（`src/config.rs`，可按需构造 `Config`）

| 配置项 | 默认值 | 触发错误 |
|---|---|---|
| `max_record_fragment` | 16384（2^14，RFC 明文记录上限） | `RecordTooLarge` |
| `max_handshake_message` | 1048576（2^20，握手消息上限） | `HandshakeTooLarge` |
| `max_client_hello` | 65536 | `ClientHelloTooLarge` |
| `max_extension_preview` | 16 字节 | （只影响未知扩展预览长度） |

## 4. 错误类型（`src/error.rs`）

- `Truncated { what }`：**软错误**，当前字节不够（记录 / 握手头 / 握手体未到齐）；
  在增量喂入时只是“暂存等待”，只有在 `finish()`（连接结束）时仍缺字节才作为错误上报。
- 其余为**硬错误**（结构确定违规，观察器进入粘滞的 Failed 态，不再解析后续字节）：
  `BadRecordContentType`、`BadRecordVersion`、`RecordTooLarge`、`HandshakeTooLarge`、
  `ClientHelloTooLarge`、`LengthMismatch { field, declared, actual }`（含**嵌套长度不符**）、
  `DuplicateExtension`、`InvalidValue`。

## 5. “不把密文误解析为握手”是如何保证的

1. 只在 `AwaitHello` 阶段、且记录 `ContentType == 22(handshake)` 时才解释 fragment；
2. 连接开头若是 `application_data`（密文先行），直接结论
   `encrypted_data_without_handshake`，**绝不**扫描其载荷寻找 `0x16`；
3. ClientHello 一旦确认（进入 Done 终态），后续 CCS / app_data **只按记录头计数**，
   fragment 一律不透明——哪怕密文里恰好出现 `16 03 03 ..` 的“伪记录头”；
4. 握手消息重组途中若插入非握手记录（明文结构被破坏 / 加密开始），结论
   `record_interleaved`，拒绝继续猜测内容；
5. 500 组随机记录 + 200 组随机 app_data 的模糊测试断言：**任何随机输入都不得产出
   ClientHello、也不得 panic**（见 `tests/ciphertext.rs`）。

## 6. 构建

```bash
cargo build --release
# 产物：
#   target/release/tls-observer-server
#   target/release/gen-samples
#   target/release/observe-file
```

工具链：在 `rustc 1.98.1 (stable)` 上开发；无网络依赖即可编译（零 crate）。

## 7. 使用

### 7.1 本地 TCP 测试服务

```bash
./target/release/tls-observer-server 127.0.0.1:9000
# 可选：--read-timeout-ms N（每条连接静默超时，默认 5000ms）
```

服务**不做 TLS 握手**，只是把收到的裸字节喂给观察器；每条连接结束（EOF / 超时 /
解析错误）时向 **stdout** 打印一行 JSON，运行日志在 stderr。

发送合成报文：

```bash
# 一次性发送
python3 scripts/feed_sample.py 127.0.0.1 9000 samples/01_valid_single_record.bin
# 按每 3 字节分片发送（验证增量重组），片间 1ms
python3 scripts/feed_sample.py 127.0.0.1 9000 samples/01_valid_single_record.bin 3 0.001
```

观察真实客户端的明文 ClientHello（服务端不应答握手，客户端会超时报错，属预期）：

```bash
openssl s_client -connect 127.0.0.1:9000 \
    -servername real-openssl.example.org -alpn h2,http/1.1 -tls1_3
```

一条成功报告的字段：

```json
{"peer_addr":"127.0.0.1:39200","bytes_received":276,"termination":"timeout",
 "result":"client_hello",
 "client_hello":{"legacy_version":"0x0303",
   "cipher_suites":["0x1302","0x1303","0x1301","0x00ff"],
   "grease_cipher_suites":[],
   "sni":"real-openssl.example.org","alpn":["h2","http/1.1"],
   "supported_versions":["0x0304"],"grease_versions":[],"grease_extensions":[],
   "unknown_extensions":[{"ext_type":"0x0033","data_len":38,"data_preview":"0024001d0020..."}]},
 "record_count":1}
```

> 注：协议数值（版本/套件/扩展类型）在 JSON 中是**带引号的十六进制字符串**
> （JSON 数字不支持 `0x` 字面量）。

失败 / 无 ClientHello 的报告：

```json
{"result":"parse_error","error":"duplicate extension of type 0x0000 ..."}
{"result":"parse_error","error":"length mismatch in `server_name_list`: declared 100 byte(s), found 6"}
{"result":"no_client_hello","no_client_hello_reason":"encrypted_data_without_handshake"}
```

### 7.2 离线文件观察器

```bash
./target/release/observe-file samples/02_handshake_split_records.bin
./target/release/observe-file samples/01_valid_single_record.bin 7   # 每 7 字节喂一次
echo $?   # 观察到/无 ClientHello=0；硬解析错误=1
```

### 7.3 生成合成报文样例

```bash
./target/release/gen-samples samples   # 生成 16 个 .bin 与 samples/MANIFEST.txt
```

### 7.4 一键端到端演示

```bash
bash scripts/demo.sh 127.0.0.1:9443
# 发送全部 16 个样例 + 逐字节重发样例13 + openssl 真实 ClientHello
# 报告写入 demo_report.jsonl（仓库内留存副本 examples/sample_output.jsonl）
```

## 8. 作为库使用

```rust
use tls_observer::{Config, Observer, Conclusion};

let mut obs = Observer::new(Config::default());
for chunk in tcp_stream_chunks {        // 任意大小、任意切分边界
    obs.feed(&chunk)?;                  // 硬错误返回 Err；未到齐只是内部暂存
}
match obs.finish()? {                    // 连接结束，处理残留截断
    Conclusion::ClientHello(ch) => {
        println!("sni = {:?}", ch.sni);
        println!("alpn = {:?}", ch.alpn);
        println!("unknown extensions = {:?}",
                 ch.unknown_extensions.iter().map(|u| u.ext_type).collect::<Vec<_>>());
    }
    Conclusion::NoClientHello(reason) => println!("no hello: {}", reason.as_code()),
}
```

## 9. 测试

```bash
cargo test            # 39 个测试（6 单元 + 33 集成；含真实 TCP 回环 E2E）
cargo clippy --all-targets   # 无警告
cargo fmt --check
```

测试矩阵：

| 文件 | 覆盖 |
|---|---|
| `tests/observer_basic.rs` | 单记录/跨 3 记录重组、9 种 TCP 切分大小一致性、逐字节、头逐字节、截断、空连接、CCS/Alert 在前 |
| `tests/malformed.rs` | 重复扩展、嵌套长度不符、扩展块越界、记录/握手超长、非法 content type/版本、奇长套件、错误粘滞 |
| `tests/grease.rs` | 16 个 GREASE 值全槽位、重复 GREASE、近似值（0x2A3A 等）不算 GREASE |
| `tests/ciphertext.rs` | hello 后密文不被二次解析、密文先行、500+200 组随机模糊、重组途中插记录 |
| `tests/server_e2e.rs` | 真实 TCP 回环：一次写、跨写边界、纯密文、解析错误 JSON |

## 10. 合成报文样例（samples/）

| 文件 | 场景 | 实际结果 |
|---|---|---|
| 01_valid_single_record | 完整明文 ClientHello（SNI/ALPN/GREASE/未知扩展） | `client_hello` |
| 02_handshake_split_records | 同一握手消息跨 3 条记录（切点跨握手头） | `client_hello`（重组） |
| 03_duplicate_extension | SNI 扩展出现两次 | `parse_error: DuplicateExtension(0x0000)` |
| 04_nested_length_mismatch_sni | SNI 内层 list 声明 100，外层仅 8 | `parse_error: LengthMismatch(server_name_list)` |
| 05_grease_values | GREASE 在扩展/套件/版本多槽位 | `client_hello`，GREASE 被剔除 |
| 06_truncated_record | 记录声明长度比实际多 40 后断连 | `parse_error: Truncated(record)` |
| 07_ciphertext_after_hello | hello 后接 CCS+随机 app_data | `client_hello`，密文不二次解析 |
| 08_encrypted_only | 开头就是随机 app_data（含 0x16 模式） | `no_client_hello: encrypted_data_without_handshake` |
| 09_record_too_large | fragment 声明 16385 | `parse_error: RecordTooLarge` |
| 10_bad_content_type | content_type=30 | `parse_error: BadRecordContentType(30)` |
| 11_first_message_server_hello | 首条握手是 ServerHello | `no_client_hello: first_handshake_not_client_hello` |
| 12_bad_record_version | version=0x0002 | `parse_error: BadRecordVersion` |
| 13_valid_for_tiny_tcp_segments | 任意字节边界喂入均一致 | `client_hello` |
| 14_empty_connection | 零字节 | `no_client_hello: connection_ended` |
| 15_truncated_handshake_header | 握手头仅 3 字节即断 | `parse_error: Truncated(handshake_header)` |
| 16_ccs_alert_then_hello | CCS/Alert 先于 ClientHello | `client_hello`（前两条非致命） |

## 11. 安全与局限的诚实说明

- 本工具只看明文。TLS 1.3 的 ClientHello 在真实网络上是明文（加密握手之后才开始），
  因此 SNI/ALPN 可见；ESNI/ECH 加密的 SNI 无法观察，会体现在相关扩展为未知/不解码。
- 它不验证对端身份、不防主动规避，畸形但落在子集外的结构会被明确判错而非猜测。
- “观察器”是被动分析组件；请仅在你拥有授权的链路 / 测试环境中使用。
