# 请求样例

由 `cargo run --example gen_samples` 生成。每行为报文的十六进制转储。

配合服务测试：`./scripts/demo.sh`，或手动：

```sh
cargo run --release --bin dns-tcp-server &
cargo run --example client -- 127.0.0.1:10053 samples/query_a.bin
```

| 文件 | 说明 | 预期 |
|---|---|---|
| `query_a.bin` | 标准 A 查询（无压缩） | NOERROR，回答 192.0.2.1 |
| `query_aaaa.bin` | 标准 AAAA 查询 | NOERROR，回答 2001:db8::1 |
| `query_cname.bin` | 标准 CNAME 查询 | NOERROR，回答 alias.example.com. |
| `query_root.bin` | 根域名（.）A 查询 | NOERROR，回答 192.0.2.1 |
| `response_alias_compressed.bin` | 压缩响应：CNAME 记录，名字与目标均用回指指针 | QR=1 回显：解码后重新编码（非压缩），语义不变 |
| `response_forward_pointer.bin` | 前向指针：QNAME 指向其后 RDATA 中的名字（合法但不常见） | 解码成功：QNAME=www.example.com.，CNAME=example.com.；回显为语义等价的非压缩报文 |
| `response_unknown_type.bin` | 未知类型（TYPE99）响应：RDATA 原样保留字节 | QR=1 回显：RDATA 字节逐字节一致 |
| `bad_loop_self.bin` | 畸形：QNAME 指针指向自身（自环） | FORMERR（PointerLoop） |
| `bad_loop_pair.bin` | 畸形：两个指针互相指向（互环） | FORMERR（PointerLoop） |
| `bad_label_overrun.bin` | 畸形：标签长度越过报文末尾 | FORMERR（UnexpectedEof） |
| `bad_truncated_rr.bin` | 畸形：RDLENGTH 声明 8 字节但报文只剩 4 字节 | FORMERR（RdataLengthMismatch） |
| `bad_reserved_label.bin` | 畸形：标签使用保留前缀 01 | FORMERR（ReservedLabelKind） |
| `bad_extended_label.bin` | 畸形：标签使用扩展前缀 10（EDNS 扩展标签） | FORMERR（UnsupportedExtendedLabel） |
| `stress_pointer_chain.bin` | 压力：71 跳指针链（默认上限 64） | FORMERR（PointerChainTooLong）；上限调到 128 可正常解码 |

## query_a.bin

标准 A 查询（无压缩）

预期：NOERROR，回答 192.0.2.1

```
0000: 12 34 01 00 00 01 00 00 00 00 00 00 03 77 77 77 
0010: 07 65 78 61 6d 70 6c 65 03 63 6f 6d 00 00 01 00 
0020: 01 
```

## query_aaaa.bin

标准 AAAA 查询

预期：NOERROR，回答 2001:db8::1

```
0000: 12 35 01 00 00 01 00 00 00 00 00 00 03 77 77 77 
0010: 07 65 78 61 6d 70 6c 65 03 63 6f 6d 00 00 1c 00 
0020: 01 
```

## query_cname.bin

标准 CNAME 查询

预期：NOERROR，回答 alias.example.com.

```
0000: 12 36 01 00 00 01 00 00 00 00 00 00 03 77 77 77 
0010: 07 65 78 61 6d 70 6c 65 03 63 6f 6d 00 00 05 00 
0020: 01 
```

## query_root.bin

根域名（.）A 查询

预期：NOERROR，回答 192.0.2.1

```
0000: 12 37 01 00 00 01 00 00 00 00 00 00 00 00 01 00 
0010: 01 
```

## response_alias_compressed.bin

压缩响应：CNAME 记录，名字与目标均用回指指针

预期：QR=1 回显：解码后重新编码（非压缩），语义不变

```
0000: 9a bc 81 80 00 01 00 01 00 00 00 00 03 77 77 77 
0010: 07 65 78 61 6d 70 6c 65 03 63 6f 6d 00 00 05 00 
0020: 01 c0 0c 00 05 00 01 00 00 01 2c 00 08 05 61 6c 
0030: 69 61 73 c0 10 
```

## response_forward_pointer.bin

前向指针：QNAME 指向其后 RDATA 中的名字（合法但不常见）

预期：解码成功：QNAME=www.example.com.，CNAME=example.com.；回显为语义等价的非压缩报文

```
0000: f0 0d 81 80 00 01 00 01 00 00 00 00 03 77 77 77 
0010: c0 22 00 01 00 01 c0 0c 00 05 00 01 00 00 00 3c 
0020: 00 0d 07 65 78 61 6d 70 6c 65 03 63 6f 6d 00 
```

## response_unknown_type.bin

未知类型（TYPE99）响应：RDATA 原样保留字节

预期：QR=1 回显：RDATA 字节逐字节一致

```
0000: 77 77 81 80 00 01 00 01 00 00 00 00 07 65 78 61 
0010: 6d 70 6c 65 03 63 6f 6d 00 00 63 00 01 c0 0c 00 
0020: 63 00 01 00 00 01 2c 00 0b 76 3d 73 70 66 31 20 
0030: 2d 61 6c 6c 
```

## bad_loop_self.bin

畸形：QNAME 指针指向自身（自环）

预期：FORMERR（PointerLoop）

```
0000: ba d1 01 00 00 01 00 00 00 00 00 00 c0 0c 00 01 
0010: 00 01 
```

## bad_loop_pair.bin

畸形：两个指针互相指向（互环）

预期：FORMERR（PointerLoop）

```
0000: ba d2 01 00 00 01 00 00 00 00 00 00 c0 0e c0 0c 
0010: 00 01 00 01 
```

## bad_label_overrun.bin

畸形：标签长度越过报文末尾

预期：FORMERR（UnexpectedEof）

```
0000: ba d3 01 00 00 01 00 00 00 00 00 00 0a 61 62 
```

## bad_truncated_rr.bin

畸形：RDLENGTH 声明 8 字节但报文只剩 4 字节

预期：FORMERR（RdataLengthMismatch）

```
0000: ba d4 81 80 00 01 00 01 00 00 00 00 07 65 78 61 
0010: 6d 70 6c 65 03 63 6f 6d 00 00 01 00 01 c0 0c 00 
0020: 01 00 01 00 00 00 3c 00 08 c0 00 02 01 
```

## bad_reserved_label.bin

畸形：标签使用保留前缀 01

预期：FORMERR（ReservedLabelKind）

```
0000: ba d5 01 00 00 01 00 00 00 00 00 00 40 78 78 
```

## bad_extended_label.bin

畸形：标签使用扩展前缀 10（EDNS 扩展标签）

预期：FORMERR（UnsupportedExtendedLabel）

```
0000: ba d6 01 00 00 01 00 00 00 00 00 00 80 78 78 
```

## stress_pointer_chain.bin

压力：71 跳指针链（默认上限 64）

预期：FORMERR（PointerChainTooLong）；上限调到 128 可正常解码

```
0000: be ef 81 80 00 00 00 01 00 00 00 00 c0 a6 00 01 
0010: 00 01 00 00 00 3c 00 04 c0 00 02 01 c0 a8 c0 1c 
0020: c0 1e c0 20 c0 22 c0 24 c0 26 c0 28 c0 2a c0 2c 
0030: c0 2e c0 30 c0 32 c0 34 c0 36 c0 38 c0 3a c0 3c 
0040: c0 3e c0 40 c0 42 c0 44 c0 46 c0 48 c0 4a c0 4c 
0050: c0 4e c0 50 c0 52 c0 54 c0 56 c0 58 c0 5a c0 5c 
0060: c0 5e c0 60 c0 62 c0 64 c0 66 c0 68 c0 6a c0 6c 
0070: c0 6e c0 70 c0 72 c0 74 c0 76 c0 78 c0 7a c0 7c 
0080: c0 7e c0 80 c0 82 c0 84 c0 86 c0 88 c0 8a c0 8c 
0090: c0 8e c0 90 c0 92 c0 94 c0 96 c0 98 c0 9a c0 9c 
00a0: c0 9e c0 a0 c0 a2 c0 a4 00 
```

