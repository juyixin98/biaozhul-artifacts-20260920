# 请求样例（Request Cookbook）

`tls-observer-server` 是**裸 TCP** 服务（不是 HTTP）：把按 TCP 发送顺序排列的
字节直接写进连接即可。`samples/*.bin` 就是可直接发送的请求字节。

所有例子假设服务已启动：

```bash
./target/release/tls-observer-server 127.0.0.1:9000
```

服务端对每条连接回一行 JSON（在服务端 stdout，不会写回 socket）。

## 1. 发送合成报文（推荐，跨平台）

```bash
# 一次性发送整个报文，然后半关闭写端（服务端读到 EOF 出报告）
python3 scripts/feed_sample.py 127.0.0.1 9000 samples/01_valid_single_record.bin

# 按每 3 字节一个 TCP 段发送，片间 1ms —— 验证增量/跨段重组
python3 scripts/feed_sample.py 127.0.0.1 9000 samples/02_handshake_split_records.bin 3 0.001

# 逐字节发送极端分片场景
python3 scripts/feed_sample.py 127.0.0.1 9000 samples/13_valid_for_tiny_tcp_segments.bin 1 0.001
```

## 2. nc / bash 发送原始字节

```bash
# 报文是二进制，用 nc 直接灌文件；-q0 在 EOF 后关闭连接
nc 127.0.0.1 9000 -q0 < samples/05_grease_values.bin
nc 127.0.0.1 9000 -q0 < samples/03_duplicate_extension.bin   # 服务端输出 parse_error
```

## 3. 真实 TLS 客户端的明文 ClientHello

服务端**不会**完成握手，客户端最终超时报错——但在它发出 ClientHello 的那一刻，
观察器已经记录到 SNI/ALPN：

```bash
openssl s_client -connect 127.0.0.1:9000 \
    -servername real-openssl.example.org \
    -alpn h2,http/1.1 -tls1_3 </dev/null
```

服务端报告（节选）：

```json
{"termination":"timeout","result":"client_hello",
 "client_hello":{"sni":"real-openssl.example.org","alpn":["h2","http/1.1"],
 "supported_versions":["0x0304"],
 "unknown_extensions":[{"ext_type":"0x000b",...},{"ext_type":"0x0033",...}]}}
```

## 4. 离线请求（不起服务）

`observe-file` 直接对文件做观察，第二个参数是模拟的读入块大小：

```bash
./target/release/observe-file samples/01_valid_single_record.bin
./target/release/observe-file samples/04_nested_length_mismatch_sni.bin; echo "exit=$?"   # 1
./target/release/observe-file samples/08_encrypted_only.bin                               # no_client_hello
```

## 5. 请求 → 预期响应对照表

| 请求载荷 | 服务端 `result` | 关键字段 |
|---|---|---|
| 01_valid_single_record.bin | `client_hello` | `sni=example.com`, `alpn=[h2,http/1.1]` |
| 02_handshake_split_records.bin | `client_hello` | 跨 3 记录重组后 SNI 同上 |
| 03_duplicate_extension.bin | `parse_error` | `duplicate extension of type 0x0000` |
| 04_nested_length_mismatch_sni.bin | `parse_error` | `length mismatch in server_name_list` |
| 05_grease_values.bin | `client_hello` | GREASE 全部被过滤，无重复扩展错误 |
| 06_truncated_record.bin | `parse_error` | `truncated: incomplete record` |
| 07_ciphertext_after_hello.bin | `client_hello` | `record_count=3`，仅第 1 条 `parsed_as_plaintext=true` |
| 08_encrypted_only.bin | `no_client_hello` | `encrypted_data_without_handshake` |
| 09_record_too_large.bin | `parse_error` | `fragment too large ... limit is 16384` |
| 10_bad_content_type.bin | `parse_error` | `unsupported content type: 30` |
| 11_first_message_server_hello.bin | `no_client_hello` | `first_handshake_not_client_hello`, type=2 |
| 12_bad_record_version.bin | `parse_error` | `unsupported record layer version: 0x0002` |
| 14_empty_connection.bin | `no_client_hello` | `connection_ended` |
| 15_truncated_handshake_header.bin | `parse_error` | `truncated: incomplete handshake_header` |
| 16_ccs_alert_then_hello.bin | `client_hello` | CCS/Alert 非致命 |

一份真实运行的完整 18 行输出保存在 [`sample_output.jsonl`](sample_output.jsonl)。
