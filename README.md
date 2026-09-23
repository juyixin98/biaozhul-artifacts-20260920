# dns_compress — DNS 压缩名字解码（纯后端）

用 Rust 从零实现的 DNS（RFC 1035）**压缩名字（§4.1.4）增量字节解析库**，
附带一个本地 TCP 测试服务。**不使用任何第三方协议解析库**（零依赖，仅标准库）；
报文解析、指针解引用、报文编码均为手写实现。

## 明确的支持子集

| 维度 | 支持范围 |
|---|---|
| 记录类型 | A(1)、AAAA(28)、CNAME(5) 解析为结构化 RDATA；**其他所有未知类型按 RDLENGTH 原样保留字节**，重编码时逐字节写回 |
| 压缩指针 | 完整支持 `11xxxxxx` 双字节指针；跳转次数可配（默认 64）；逐偏移环检测（自环/互环都检出）；目标偏移越界检测；**允许并正确跟随前向指针** |
| 标签 | 普通标签（`00xxxxxx`，≤63 字节）、根零字节；保留前缀 `01` 与扩展标签前缀 `10`（EDNS label）一律以明确错误拒绝 |
| 长度上限 | 标签 63B、名字在线格式 255B、标签数（默认 127）、每区段记录数（默认 4096）、TCP 单帧长度（服务端默认 4096B）均可配置 |
| 编码 | 手写编码器，输出**不使用压缩**（合法的非压缩形式），未知类型 RDATA 原样写回 |
| 传输 | RFC 1035 §4.2.2 TCP 两字节长度前缀帧；增量读取，区分干净 EOF / 帧截断 / 声明超长 |
| 不支持 | EDNS OPT、TSIG、区传送、DNSSEC、压缩编码输出（均为有意的范围裁剪） |

## 错误类型

所有解析失败都映射为 `dns_compress::DnsError` 的明确变体（见 `src/error.rs`）：

`UnexpectedEof`、`MessageTooShort`、`MessageTooLong`、`ReservedLabelKind`、
`UnsupportedExtendedLabel`、`PointerLoop`、`PointerChainTooLong`、
`PointerOutOfBounds`、`TruncatedPointer`、`LabelTooLong`、`NameTooLong`、
`TooManyLabels`、`EmptyLabel`、`RdataLengthMismatch`、`InvalidRdataLen`、
`InvalidRdataName`、`TooManyRecords`。

TCP 帧层另有 `frame::FrameError`：`Io` / `MessageTooLong` / `UnexpectedEof` / `Closed`。

## 目录结构

```
src/
  lib.rs        模块导出
  error.rs      DnsError 错误枚举
  parser.rs     游标式增量字节解析器 Reader + Limits 上限配置
  name.rs       Name：标签、压缩指针安全解码、文本/二进制编码
  message.rs    Message/Question/ResourceRecord/Rdata：解析与编码
  frame.rs      TCP 两字节长度前缀成帧（增量读）
  server.rs     与 I/O 解耦的应答构造逻辑 respond()
  bin/server.rs TCP 服务入口（多线程）
examples/
  client.rs     发送 .bin 报文并解码打印响应的最小客户端
  gen_samples.rs 生成 samples/ 下全部样例（含 hex 文档）
tests/
  decode_tests.rs  24 项：前向指针/越界标签/指针环/截断 RR/上限/语义往返…
  server_tests.rs  4 项：应答规则 + 真实 TCP 端到端 + 超长/截断帧
samples/          14 个报文样例与 README（由 gen_samples 生成）
scripts/demo.sh   一键构建、起服务、逐个发样例
```

## 快速开始

```sh
cargo test                 # 全部自动化测试（35 项）
cargo run --release --bin dns-tcp-server          # 启动服务 127.0.0.1:10053
cargo run --example gen_samples                   # 重新生成 samples/
cargo run --example client -- 127.0.0.1:10053 samples/query_a.bin
./scripts/demo.sh                                 # 一键端到端演示
```

服务参数：`dns-tcp-server [绑定地址] [最大报文长度]`（默认 `127.0.0.1:10053 4096`）。

## 测试服务行为

- **查询（QR=0）**：第一个问题为 A / AAAA / CNAME 时回固定记录
  （`192.0.2.1`、`2001:db8::1`、`alias.example.com.`，TTL 60）；其他类型回 `NOTIMP`(4)。
- **响应（QR=1）**：解析后重新编码原样返回——压缩输入变为非压缩输出，
  用于端到端验证“解码→编码语义往返”。
- **畸形报文**：回 `FORMERR`(1)，ID 尽量从原始前两字节回显。
- 同一 TCP 连接可连续发送多个长度前缀帧（流水线复用）。

## 压缩指针的安全要点

1. 指针目标是**报文内绝对偏移**，解引用前校验 `< 报文总长`，否则
   `PointerOutOfBounds`（且指针只允许指向同一份报文，天然不能越出输入）。
2. 每跟随一次跳转计数 +1，超过 `Limits::max_pointer_jumps`（默认 64）即
   `PointerChainTooLong`，防止伪造长链放大 CPU。
3. 维护已访问偏移列表，重复访问即 `PointerLoop`——自环（指向自身）与
   多节点互环都在第二次访问时被检出。
4. 指针第二个字节缺失给专门的 `TruncatedPointer`；普通标签越过报文尾给
   `UnexpectedEof`。
5. 名字物理消耗（标签序列 + 终止根或末尾指针）用于外层 RR 精确推进；
   CNAME 的 RDATA 名字物理消耗必须**恰好等于 RDLENGTH**，防止名字吞掉
   下一条记录的字节。

## 语义往返语义

- 无压缩输入：解析→编码后**逐字节一致**（测试 `uncompressed_query_is_byte_identical_after_roundtrip`）。
- 有压缩输入：输出为更长但语义等价的非压缩报文；再次解析得到的 `Message`
  与首次解析结果相等（测试 `semantic_roundtrip_of_compressed_response`、
  `forward_pointer_is_followed`）。
- 未知类型 RDATA：作为不透明字节保留，往返一致（`unknown_rdata_preserved_as_bytes`）。

## 工具链

Rust stable 1.98（edition 2021），零第三方依赖。
