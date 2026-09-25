# dns-compress — DNS 压缩名字增量解码库 + 本地 TCP 测试服务

纯 Rust（**仅标准库**，零第三方依赖）从零实现的 DNS 压缩名字（RFC 1035 §4.1.4）
增量字节解析库，附带一个用于本地验证的 TCP DNS 服务与命令行客户端。

> 本项目**没有使用也没有包装任何现成 DNS 协议解析器**；头部、名字解压、
> RDATA 解析全部在不可变字节缓冲上手写完成。不包含前端。

## 功能与明确支持范围

### 增量字节解析库（`dns_compress`）

- `reader::Reader`：游标式增量读取器，支持大端 u8/u16/u32、定长切片、
  以及“共享同一完整报文、带 RDLENGTH 上界”的子读取器。
- 压缩名字解压（`name::Name::parse`）：
  - 普通标签（长度 ≤ 63）、根名、名字线格式总长 **≤ 255**；
  - 压缩指针（`0b11` 前缀）：
    - 指针必须指向**报文内部**，否则报 `PointerOutOfBounds`；
    - **明确支持前向指针**（目标在当前位置之后，例如指向后续 RDATA）；
    - 跳转次数上限 `MAX_POINTER_JUMPS = 64`，超出报 `TooManyPointers`；
    - 环检测：重复访问同一指针目标立即报 `PointerLoop`（对自环、双节点环、
      长链回环均生效）；
  - `0b01/0b10` 保留标签类型明确报 `UnsupportedLabelType`。
- 报文解析（`message::Message::parse`）：
  - 完整 12 字节头部（flags 按位保留 QR/Opcode/AA/TC/RD/RA/Z/RCODE）；
  - 问题段任意 QTYPE/QCLASS；
  - 资源记录 RDATA **仅结构化支持 A（1）、AAAA（28）、CNAME（5）**；
  - **未知类型原样保留 RDATA 字节**（`Rdata::Unknown { rtype, raw }`），
    再编码时逐字节还原；
  - 报文长度上限 `MAX_MESSAGE_LEN = 65535`；
  - 截断的头部 / 问题 / 资源记录、计数与实际条目不符、尾部多余字节
    均返回明确错误（见下）。
- 有限响应编码（`Message::encode`）：
  - 按 RFC 1035 规则输出**压缩指针**（对已出现的名字后缀生成指针）；
  - 自动重算四个计数字段；同样受 65535 长度上限约束。

### 错误类型一览（`error::DnsError`）

| 变体 | 触发场景 |
|---|---|
| `UnexpectedEof { offset, len }` | 游标读取越过报文/子区间末尾（截断的头、问题、标签内容等） |
| `LabelTooLong { len, max }` | 标签 > 63 字节，或名字线长 > 255 |
| `InvalidName` | 文本构造域名时非法（空标签等） |
| `PointerOutOfBounds { target, msg_len }` | 压缩指针目标超出报文范围 |
| `TooManyPointers { jumps }` | 指针跳转超过 64 次 |
| `PointerLoop { offset }` | 指针目标重复访问（环） |
| `UnsupportedLabelType { prefix }` | 0b01/0b10 保留标签类型 |
| `TruncatedRecord { at, declared, actual }` | RDLENGTH 声明长度超出报文，或 A/AAAA 长度不符 |
| `CountExceedsData` / `Framing` | 计数条目缺失 / 尾部多余字节等帧级问题 |
| `MessageTooLong { len, max }` | 输入或编码结果超过 65535 |

### TCP 测试服务（`dns-server`）

- RFC 1035 §4.2.2 分帧：每条消息 2 字节大端长度前缀 + DNS 报文；
- 内置静态测试区：

  | 名字 | 记录 |
  |---|---|
  | `localhost.test` | A 127.0.0.1，AAAA ::1 |
  | `www.example.test` | CNAME `cdn.example.test` |
  | `cdn.example.test` | A 192.0.2.10，AAAA 2001:db8::a |
  | `a.example.test` | A 192.0.2.1 |
  | `aaaa.example.test` | AAAA 2001:db8::1 |

- 查询处理：
  - QTYPE=A/AAAA/CNAME 且 QCLASS=IN：按区应答；A/AAAA 会跟随 CNAME 链
    （链中每跳都写入 CNAME 记录，链尾给地址记录）；
  - 其他 QTYPE/QCLASS：`NOERROR` 空答案（有限子集不实现）；
  - 未知名字：`NXDOMAIN`；解析失败且能恢复 ID：`FORMERR`；
  - 应答统一走库的压缩编码器，保证输出合法压缩指针；
  - 每个连接一个线程，读超时 10 秒，同一连接可处理多条消息。

## 目录结构

```text
Cargo.toml
src/
  lib.rs            库入口与公开 API
  error.rs          DnsError 错误枚举
  reader.rs         增量字节读取器（绝对偏移 + 子区间上界）
  name.rs           Name：压缩解压（跳转上限/环检测/前向指针）与压缩编码
  message.rs        报文解析与有限响应编码（A/AAAA/CNAME/未知原字节）
  server.rs         静态区与 TCP 服务
  bin/server.rs     dns-server 可执行文件
  bin/client.rs     dns-client 可执行文件（query/raw/gen-samples/decode-file）
tests/
  name_test.rs      名字解压：前向指针、越界标签、指针环、跳转上限等 14 项
  message_test.rs   报文解析、截断 RR、已知报文字节级/语义往返等 9 项
  server_test.rs    TCP 端到端：A/AAAA/CNAME 链、NXDOMAIN、FORMERR 等 8 项
samples/            8 个请求/畸形样例（.bin 含长度前缀，.hex.txt 为十六进制）
  send_sample.py    把样例原样发给服务的小脚本
RUNLOG.md           实际构建/测试/运行命令与结果记录
```

## 构建与测试

需要 Rust 稳定版工具链（开发环境为 cargo/rustc 1.98.1，无外部依赖）。

```bash
cargo build                 # 调试构建
cargo test                  # 全部自动化测试（31 项）
cargo clippy --all-targets  # 静态检查（无警告）
cargo build --release       # 发布构建
```

## 使用示例

```bash
# 启动服务（默认 127.0.0.1:1053，可传参覆盖；若 1053 被占用请换端口）
./target/debug/dns-server 127.0.0.1:11053

# 另开终端：查询
./target/debug/dns-client query localhost.test A 127.0.0.1:11053
./target/debug/dns-client query www.example.test A 127.0.0.1:11053
./target/debug/dns-client query aaaa.example.test AAAA 127.0.0.1:11053

# 解码样例文件（本地解析，不发网络；--raw 表示文件无 TCP 长度前缀）
./target/debug/dns-client decode-file samples/forward-pointer.bin
./target/debug/dns-client decode-file samples/pointer-loop.bin   # 应报 PointerLoop

# 把样例通过真实 TCP 发给服务
python3 samples/send_sample.py samples/pointer-loop.bin 127.0.0.1:11053

# 重新生成样例
./target/debug/dns-client gen-samples samples
```

## 设计说明与取舍

- **前向指针为何允许**：RFC 1035 只要求指针指向“本消息中先前出现的名字”，
  但真实实现与测试要求覆盖前向指针。本解压器对目标只做“在报文内”校验，
  终止性由“已访问指针集合（环检测）+ 64 次跳转上限”共同保证，
  因此接受前向指针不会引入无限循环。
- **子读取器仍共享完整报文**：CNAME 的 RDATA 中的压缩指针按 RFC 可指向
  报文任意更早位置，因此 `Reader` 不截断底层缓冲，而是用
  `[start, end)` 上界约束顺序读取，名字解压按完整报文的绝对偏移寻址。
- **往返语义**：验收的“decode→encode 语义往返”以*再次解析后结构相等*
  为准（压缩布局可能与原始报文不同）；第二次编码与第一次编码字节相同
  （幂等）。对服务端实际应答，还额外验证了与原应答字节完全一致。
- **非目标**：不实现 UDP、DNSSEC/EDNS(OPT 会作为未知类型原字节保留)、
  IDN/punycode、递归转发与权威区文件加载；这些超出有限子集范围。
