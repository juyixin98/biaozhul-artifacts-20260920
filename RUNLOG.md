# 运行记录（RUNLOG）

本文件如实记录开发与验收过程中**实际执行的命令、结果，以及中途出现并修复的问题**。
不省略失败项；最终状态见文末“验收结论”。

- 日期：2026-09-23
- 平台：Linux 6.8.0-90-generic (x86_64)
- 工具链：`rustc 1.98.1 (48a229cea 2026-09-01)` / `cargo 1.98.1`（stable）
- 第三方依赖：**0**（`[dependencies]` 为空，核心解析纯 `std` 手写）

---

## 一、最终验收命令与结果

### 1. 编译

```
$ cargo build --release
    Finished `release` profile [optimized] target(s)
$ cargo build
    Finished `dev` profile
```

均成功，无警告。

### 2. 自动化测试

```
$ cargo test
```

实际结果（逐行）：

```
running 6 tests   (src 单元测试: reader×4, grease×2)                 ... ok
running 4 tests   (tests/ciphertext.rs)                            ... ok
running 4 tests   (tests/grease.rs)                                ... ok
running 11 tests  (tests/malformed.rs)                             ... ok
running 10 tests  (tests/observer_basic.rs)                        ... ok
running 4 tests   (tests/server_e2e.rs，真实 TCP 回环)             ... ok
Doc-tests                                                          ... 0
test result: ok. 6+4+4+11+10+4 = 39 passed; 0 failed
```

- 未通过项：**无**。
- E2E 用时约 0.12s（跨写边界用例含 200µs 睡眠）。

### 3. 静态检查与格式

```
$ cargo clippy --all-targets     # Finished，无 warning
$ cargo fmt --check              # 无 diff（fmt ok）
```

### 4. 16 个合成报文样例：实际结果 vs 预期

命令：对每个文件运行 `./target/release/observe-file samples/NN.bin`
（退出码：观察到/无 ClientHello=0，硬解析错误=1）

| 样例 | 退出码 | 实际 result | 与预期 |
|---|---|---|---|
| 01_valid_single_record | 0 | client_hello | ✅ |
| 02_handshake_split_records | 0 | client_hello | ✅ 跨记录重组 |
| 03_duplicate_extension | 1 | parse_error | ✅ DuplicateExtension(0x0000) |
| 04_nested_length_mismatch_sni | 1 | parse_error | ✅ LengthMismatch(server_name_list) |
| 05_grease_values | 0 | client_hello | ✅ GREASE 被过滤 |
| 06_truncated_record | 1 | parse_error | ✅ Truncated(record) |
| 07_ciphertext_after_hello | 0 | client_hello | ✅ 密文未被二次解析 |
| 08_encrypted_only | 0 | no_client_hello | ✅ encrypted_data_without_handshake |
| 09_record_too_large | 1 | parse_error | ✅ RecordTooLarge(16385>16384) |
| 10_bad_content_type | 1 | parse_error | ✅ BadRecordContentType(30) |
| 11_first_message_server_hello | 0 | no_client_hello | ✅ first_handshake_not_client_hello |
| 12_bad_record_version | 1 | parse_error | ✅ BadRecordVersion(0x0002) |
| 13_valid_for_tiny_tcp_segments | 0 | client_hello | ✅ |
| 14_empty_connection | 0 | no_client_hello | ✅ connection_ended |
| 15_truncated_handshake_header | 1 | parse_error | ✅ Truncated(handshake_header) |
| 16_ccs_alert_then_hello | 0 | client_hello | ✅ CCS/Alert 非致命 |

16/16 与 `samples/MANIFEST.txt` 预期一致。

### 5. 真实网络端到端（`scripts/demo.sh`）

```
$ bash scripts/demo.sh 127.0.0.1:9443
```

实际发送：16 个样例（整文件）+ 样例 13 逐字节（每 1B 一片、片间 1ms）+
一次真实 `openssl s_client -tls1_3 -servername real-openssl.example.org -alpn h2,http/1.1`。

产出 18 行报告，经 `python3 -m json` 逐行校验**全部为合法 JSON**，副本保存在
[`examples/sample_output.jsonl`](examples/sample_output.jsonl)。其中真实 openssl 一条：

```json
{"bytes_received":276,"termination":"timeout","result":"client_hello",
 "client_hello":{"legacy_version":"0x0303",
 "cipher_suites":["0x1302","0x1303","0x1301","0x00ff"],
 "sni":"real-openssl.example.org","alpn":["h2","http/1.1"],
 "supported_versions":["0x0304"],
 "unknown_extensions":[{"ext_type":"0x000b",...},{"ext_type":"0x000a",...},
   {"ext_type":"0x0023",...},{"ext_type":"0x0016",...},{"ext_type":"0x0017",...},
   {"ext_type":"0x000d",...},{"ext_type":"0x002d",...},{"ext_type":"0x0033",...}]}}
```

说明：
- openssl 进程最终非零退出（`demo_openssl.err` 记录其握手未完成的报错），
  **这是预期行为**——观察服务按设计不应答 ServerHello；客户端的明文 ClientHello
  已在发出瞬间被记录。demo 脚本用 `timeout 6 ... || true` 包裹。
- openssl 此构建（OpenSSL 3.x，未启用 GREASE）未带 GREASE 套件，
  因此报告里 `grease_*` 为空，属正常；合成样例 05 专门覆盖 GREASE。

### 6. 离线观察器分块喂入

```
$ ./target/release/observe-file samples/01_valid_single_record.bin 7
{"result":"client_hello", ... "sni":"example.com","alpn":["h2","http/1.1"],
 "cipher_suites":["0x1301","0x1302","0x1303","0xc02f"]}
```

每 7 字节喂入与一次性喂入结论一致。

---

## 二、开发过程中实际出现过的问题与修复（含当时的失败输出）

以下问题都在开发中真实发生，按出现顺序记录；每条都有对应自动化测试防回归。

1. **缺少模块声明导致编译失败**
   `error[E0432]: unresolved import crate::reader`（lib.rs 漏挂 `reader` 模块）。
   修复：在 `src/lib.rs` 增加 `pub mod reader;`。

2. **E2E 测试闭包/所有权编译错误**
   `closure may outlive ... borrows data`、`borrow of partially moved value: report`。
   修复：writer 闭包加 `move`；消费 `report.conclusion` 的断言改为 `as_ref()`。

3. **分片辅助函数在二进制里私有，测试不可见**
   `cannot find function split_across_records`。修复：将该构造器移入
   `src/test_support.rs` 作为 `pub fn`，供库测试与 `gen-samples` 共用。

4. **GREASE 单元测试自身写错（两次）**
   - 先写成步进 1 的循环，误报 `0x0b0a should be GREASE`；
   - 修正步进后又误写值构造，误报 `0x1a0a should be GREASE`。
   经核对 RFC 8701（GREASE 高低字节相同：`0x0A0A,0x1A1A,…,0xFAFA`），
   确认**解析器实现正确，是测试枚举写错**；最终测试改为 `v=(high<<8)|high`、
   `high += 0x10`，并增加“对整个 u16 空间穷举，恰好 16 个 GREASE”的断言。

5. **重组器用 `total_len==0` 作为“头未确定”标记，与合法零长度握手体歧义。**
   主动重写为显式枚举 `Pending::Header(Vec<u8>) | Pending::Body{..}`，
   消除歧义；补充 `zero_length_handshake_body_is_handled` 测试。

6. **终态后后续记录被直接丢弃，记录计数与预期不符。**
   测试 `app_data_after_valid_hello_is_never_reparsed` 最初失败：
   期望 hello 后的 CCS+2 条 app_data 都计数（共 4 条）且仅第 1 条为明文，
   实际只数到 1 条。修复：进入终态后改为“**不透明模式**”——继续解析 5 字节
   **记录头**做元数据计数，fragment 一律不送入握手解析器。

7. **clippy 两项提示**
   - `chunks_exact` 常量块大小 → 改用 `slice::as_chunks::<2>()`；
   - `large size difference between variants`（Conclusion 两变体体积悬殊）
     → `ClientHello(Box<ClientHelloInfo>)`。修复后 clippy 干净。

8. **JSON 输出使用了非法的十六进制数字字面量（真实运行发现的功能 bug）**
   手写 JSON 把 `cipher_suites` 等输出成 `[0x1301,...]`，而 **JSON 数字不支持 0x**。
   在对 demo 报告跑 `json.loads` 时实际报错：
   `json.decoder.JSONDecodeError: Expecting ',' delimiter: ... char 285`。
   修复：所有协议数值改为带引号字符串（`"0x1301"`），并在 E2E 中加
   `assert_valid_json_hex` 回归断言（禁止输出中出现 `[0x` / `,0x` / `:0x`）。
   修复后 18 行报告全部通过 JSON 解析。

9. **演示脚本小问题**
   `feed_sample.py` 参数个数阈值 off-by-one（正常 4 个 argv 被当成缺参），
   已改为 `< 4`；就绪探测原本会发起一条空连接污染报告，改为轮询服务端日志。

10. **observe-file 初版编译错误**
    `expected &str, found &ParseError`：JSON 转义函数需要 `&str`，
    改为传 `&e.to_string()`。

---

## 三、验收对照（需求 → 落实位置）

| 需求 | 落实 |
|---|---|
| 增量字节解析库 | `src/reader.rs` + `src/observer.rs`（任意切分 `feed`，软截断/硬错误分离） |
| 明确支持子集 | `README.md §2`；记录层 20/21/22/23、版本 0x0301..=0x0304 |
| 长度上限 | `src/config.rs` + Record/Handshake/ClientHello 三级超限错误 |
| 错误类型 | `src/error.rs`：8 个变体，`Display`/`Error` 实现 |
| 不借现成解析器 | `[dependencies]` 为空；JSON 也手写（`src/json.rs`） |
| 仅记录头 + 明文 ClientHello 离线观察器 | `record.rs` / `client_hello.rs` / `bin/observe_file.rs` |
| 跨记录握手重组 | `observer.rs` `Pending::{Header,Body}`，测试 02/13 |
| 不解密/不实现握手 | 无任何密码学代码；server 不应答；CCS 后内容不透明 |
| 输出 SNI/ALPN/未知扩展 | JSON 字段 `sni/alpn/unknown_extensions(+data 预览)` |
| 重复扩展 | `DuplicateExtension`，样例 03 |
| 嵌套长度不符 | sub-reader 越界→`LengthMismatch(field)`，样例 04 |
| GREASE | `grease.rs` 全 16 值穷举，样例 05 |
| 截断 | `Truncated` 软错误 + `finish()` 上报，样例 06/15 |
| 不把密文误解析为握手 | 状态机终态不透明 + 700 组随机模糊测试，样例 07/08 |
| 源码/README/请求样例/自动化测试 | 全部交付（README.md、examples/REQUESTS.md、tests/、samples/） |
| 实际运行并如实记录 | 本文件；openssl 真实 ClientHello 已观察到 |
| 不做前端 | 无任何前端代码/资源 |

## 四、已知限制（非失败项，属设计边界）

- 只观察第一条握手消息；ServerHello 之后不解析（也无法解析密文）。
- ECH/ESNI 加密的 SNI 不解码。
- 本地测试服务面向测试，未做 TLS 化管理端口/鉴权；不应直接暴露到不可信网络。
