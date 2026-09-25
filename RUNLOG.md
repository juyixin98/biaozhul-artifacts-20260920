# 运行记录（RUNLOG）

记录日期：2026-09-23
环境：Linux 6.8.0-90-generic（x86_64），cargo 1.98.1 / rustc 1.98.1，纯标准库无第三方依赖。

> 说明：本机 **1053 端口已被系统中其他服务占用**（`ss -ltnp` 可见
> `*:1053` 处于 LISTEN），与本项目无关；因此端到端运行使用 **127.0.0.1:11053**。

## 1. 构建

```text
$ cargo build
    Finished `dev` profile [unoptimized + debuginfo] target(s)
$ cargo build --release
    Finished `release` profile [optimized + debuginfo] target(s)
```

## 2. 静态检查

```text
$ cargo clippy --all-targets
    Checking dns-compress v0.1.0
    Finished `dev` profile
（无 warning/error）
```

## 3. 自动化测试：全部通过（31/31）

```text
$ cargo test
tests/message_test.rs ... 9 passed; 0 failed
tests/name_test.rs ...... 14 passed; 0 failed
tests/server_test.rs .... 8 passed; 0 failed
```

### 3.1 名字解压（tests/name_test.rs，14 项）

- 未压缩名字 / 根名 / 恰好 255 线长名字：通过；
- 后向压缩指针（回答 owner 指向问题名）：通过；
- **前向指针**（问题名指针指向后面附加段未知 RR 的 RDATA 中部，偏移 41）：
  整体报文解析成功，问题名解为 `host`，未知 RDATA 18 字节原样保留；
- **指针越界**（目标 256 ≥ 报文长）：`PointerOutOfBounds`；
- **指针环**：自环（12→12）与双节点环（12↔14）均报 `PointerLoop`；
- 无环长指针链（>64 跳）：`TooManyPointers`；
- **越界标签**（标签声明 60 字节实际只剩 3 字节）：`UnexpectedEof`；
- 0b01/0b10 保留标签类型：`UnsupportedLabelType`；
- 名字线长 >255：`LabelTooLong`；
- CNAME RDATA 内指针指向问题名（跨 RDLENGTH 区间引用报文更早位置）：通过。

### 3.2 报文级（tests/message_test.rs，9 项）

- **手工构造的独立“已知应答报文”**（未经本项目编码器生成，含问题、
  CNAME 记录、A 记录、未知 TYPE=40 记录）：
  - 解析语义逐项核对（ID/flags/名字/TTL/192.0.2.7/未知 RDATA 6 原始字节）：通过；
  - **decode→encode 往返**：重编码结果再解析后结构与首次完全相等，
    且第二次编码与第一次字节相同（幂等）；重编码报文不超过原报文
    （确实使用了压缩）；
- AAAA 记录往返通过；
- **截断资源记录**（RDLENGTH=4 但 RDATA 完全缺失）：`TruncatedRecord{declared:4,actual:0}`；
- A 记录 RDLENGTH=3：`TruncatedRecord`；
- 截断头部、QDCOUNT 与实际问题数不符、尾部多余字节：均返回明确错误；
- 重编码报文确实包含指向问题名的压缩指针 `c0 0c`。

### 3.3 TCP 端到端（tests/server_test.rs，8 项）

真实 `TcpListener` + 线程内服务、真实环回 TCP 连接：
A（127.0.0.1）、AAAA（::1）、CNAME 链跟随（www→cdn→192.0.2.10）、
CNAME 直接查询、NXDOMAIN、不支持 QTYPE 的空 NOERROR、大小写不敏感、
**畸形载荷返回 FORMERR 且同一连接上后续请求仍正常**：全部通过。

## 4. 样例生成

```text
$ dns-client gen-samples samples
query-a-localhost.bin    34 字节（含长度前缀）
query-aaaa.bin           37
query-cname.bin          36
forward-pointer.bin      49   前向指针合法报文
pointer-out-of-bounds.bin 20  指针目标 256 越界
pointer-loop.bin         20   指针 12→12 自环
truncated-rr.bin         25   RDLENGTH=4 但 RDATA 缺失
out-of-bounds-label.bin  18   标签声明 60 字节实际 3 字节
（每个 .bin 另有同名 .hex.txt 十六进制文本）
```

## 5. 真实服务运行记录（127.0.0.1:11053）

### 5.1 正常查询（输出原文摘录）

- `query localhost.test A` → NOERROR，回答 `localhost.test ttl=300 A 127.0.0.1`，
  应答含压缩指针 `c00c`，客户端自检“decode→encode 与原字节完全一致（幂等）”。
- `query aaaa.example.test AAAA` → `AAAA 2001:db8:0:0:0:0:0:1`（2001:db8::1）。
- `query www.example.test A` → 两条回答：
  `CNAME cdn.example.test` 与 `A 192.0.2.10`（应答中含 `c00c`、`c010` 等压缩指针）。
- `query www.example.test CNAME` → 仅返回 CNAME 记录。
- 未知名字 → NXDOMAIN（RCODE=3）。
- 手工构造的 MX（QTYPE=15）查询 → RCODE=0、ANCOUNT=0（有限子集空 NOERROR）。
- 同一 TCP 连接连续两条查询（a.example.test/A、aaaa.example.test/AAAA）：
  均正常应答，ID 分别回填 1、2。

### 5.2 畸形样例经真实 TCP 发送（samples/send_sample.py）

| 样例 | 服务行为 |
|---|---|
| pointer-loop.bin | 12 字节应答 `42428001…`，QR=1 **RCODE=1（FORMERR）** |
| pointer-out-of-bounds.bin | FORMERR |
| truncated-rr.bin | FORMERR |
| out-of-bounds-label.bin | FORMERR |
| forward-pointer.bin | **正常解析**（回显问题名 `host`），因区中无此名 RCODE=3（NXDOMAIN） |

### 5.3 本地解码样例（decode-file，不经过网络）

```text
forward-pointer.bin          成功：问题 host；附加段 root ttl=0 TYPE99
                             原始RDATA=abab…04686f737400
pointer-loop.bin             解码失败：检测到压缩指针环（偏移 12 被重复访问），退出码 1
truncated-rr.bin             解码失败：资源记录截断：偏移 23 声明 RDLENGTH=4，实际只剩 0 字节，退出码 1
out-of-bounds-label.bin      解码失败：输入截断：尝试在偏移 13 读取，超出报文长度 16，退出码 1
pointer-out-of-bounds.bin    解码失败：压缩指针越界：目标偏移 256，报文长度 18，退出码 1
```

## 6. 开发过程中发现并修复的真实缺陷（如实记录）

1. **指针第二字节游标多推进一次**：`Name::parse` 初版在压缩分支中
   `cur += 1` 与 `first_end = cur + 1` 叠加，导致名字结束位置多跳 1 字节，
   连带后续 RR 字段全部错位（表现为应答报文中 RDLENGTH 被读成 1025）。
   已删除多余推进，并以 `parses_backward_pointer_to_question`
   精确断言游标位置（22→24）防回归。
2. **`UnsupportedLabelType.prefix` 初值语义不一致**：最初存的是整个首字节
   （0x40/0x80），与“2 位前缀”文档不符；已改为右移 6 位（值 1/2），
   测试相应断言。
3. **越界切片错误信息的偏移歧义**：`read_slice_at` 最初报“结束位置”，
   文案却写“在偏移 X 读取”；已统一报起始偏移。

（另有 3 处测试自身的手工字节算术错误，均在发现是测试问题后修正测试，
未改动库代码；clippy 提示的 needless_borrow/生命周期/`is_multiple_of`
已一并清理。）

## 7. 未通过项 / 限制

- **无未通过的测试**：31 项自动化测试全部通过，clippy 无警告。
- 已知范围限制（设计如此，非缺陷）：
  - 仅 TCP（无 UDP）；仅 IN 类应答；QTYPE 仅 A/AAAA/CNAME 结构化处理，
    其他类型在服务端返回空 NOERROR，在库中作为 `Rdata::Unknown` 保留原字节；
  - OPT/EDNS、DNSSEC、IDN、区文件、递归转发、前端均未实现；
  - 默认端口 1053 在本机被无关服务占用，演示使用 11053。
