# RUNLOG —— 实际运行记录

本文件如实记录在交付环境中实际执行的命令与结果。无任何未通过项。

- 日期（UTC）：2026-09-23
- 系统：Linux 6.8.0-90-generic x86_64
- 工具链：rustc 1.98.1 (48a229cea 2026-09-01) / cargo 1.98.1 (797e8a9bc 2026-08-05)
- 依赖：**零第三方 crate**（`Cargo.toml` 无 `[dependencies]`），`#![forbid(unsafe_code)]`

## 1. 构建

```text
$ cargo build --release
   Compiling minimultipart v0.1.0
    Finished `release` profile [optimized] target(s)
```

## 2. 自动化测试 `cargo test`（共 28 个，全部通过）

```text
running 5 tests   (src 单元测试：boundary/content-type/disposition/SHA-256 标准向量/辅助函数)
test result: ok. 5 passed; 0 failed; 0 ignored
running 14 tests  (tests/integration.rs)
test result: ok. 14 passed; 0 failed; 0 ignored
running 2 tests   (tests/property.rs：300 个随机合法流随机分包 + 50 路随机噪声)
test result: ok. 2 passed; 0 failed; 0 ignored
running 6 tests   (tests/server.rs：真实 TCP 回环，含逐字节发送)
test result: ok. 6 passed; 0 failed; 0 ignored
doctest
test result: ok. 1 passed; 0 failed; 0 ignored
```

## 3. 静态检查

```text
$ cargo fmt --check        # 无差异
$ cargo clippy --all-targets -- -D warnings
    Finished `dev` profile …  # 零警告、零错误
```

## 4. 端到端验收脚本 `python3 scripts/acceptance.py`（15 项，全部通过）

脚本会自动 `cargo build --release`，启动三个不同上限的真实服务实例，
用裸 TCP socket 发包（其中一路逐字节发送，服务端 `READ_SIZE=1`）：

```text
wrote examples/binary-payload.bin (96 bytes), sha256=36dd86814cece97e1ee9d081188fd8f1076282dc0edd74a407e586986f600099
[PASS] 1a 正常多 part 返回 200
[PASS] 1b part_count=3
[PASS] 1c 空 part size=0
[PASS] 1d 二进制 part 大小与 SHA-256 一致
[PASS] 1e 正文里的近似边界没有误判（ok:true）
[PASS] 2a 逐字节发送返回 200
[PASS] 2b 逐字节发送响应与普通发送完全一致
[PASS] 3 缺结束边界 → 400 truncated
[PASS] 4 超过 part 数上限 → 413 too_many_parts
[PASS] 5 单 part 超上限 → 413 part_too_large
[PASS] 6 总大小超上限 → 413 total_too_large
[PASS] 7 头部超上限 → 413 header_too_large
[PASS] 8 缺 Content-Disposition → 400 missing_disposition
[PASS] 9 非 multipart Content-Type → 400 invalid_boundary
[PASS] 10 二进制载荷确实包含全部近似边界模式

15/15 passed
ALL ACCEPTANCE CHECKS PASSED
```

## 5. 手工 curl（真实服务，`MULTIPART_READ_SIZE=1`）

```text
$ curl -s -w "\nHTTP %{http_code}\n" http://127.0.0.1:<port>/upload \
    -F "field1=hello world" -F "empty=" \
    -F "file=@examples/binary-payload.bin;type=application/octet-stream"
{"ok":true,"parts":[{"name":"field1","filename":null,"size":11,
 "sha256":"b94d27b9934d3e08a52e52d7da7dabfac484efe37a5380ee9088f7ace2efcde9"},
{"name":"empty","filename":null,"size":0,
 "sha256":"e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"},
{"name":"file","filename":"binary-payload.bin","size":96,
 "sha256":"36dd86814cece97e1ee9d081188fd8f1076282dc0edd74a407e586986f600099"}],
"part_count":3,"total_bytes":107}
HTTP 200

$ # 半截 multipart（缺结束边界）
$ curl ... -H "Content-Type: multipart/form-data; boundary=B" --data-binary @trunc.bin
{"ok":false,"error":"truncated"}
HTTP 400
```

三个 SHA-256 均与独立计算结果一致（`hello world`、空串 SHA-256、二进制载荷文件）。

## 6. “无完整缓存”实证：200 MiB 上传

单 part 200 MiB（209,715,200 字节，内容全为 `A`），逐 1 MiB 块发送，
上传过程中轮询服务进程 `/proc/<pid>/status` 的 `VmRSS`：

```text
response: {"ok":true,"parts":[{"name":"big","filename":"big.bin","size":209715200,
 "sha256":"fb3a4ee074b0138c7904489e5fd3d26fdc28d4d2061cb18e6d2b126f5242be99"}],
 "part_count":1,"total_bytes":209715200}
server peak RSS during 200MiB upload: 2048 KiB = 2.0 MiB
```

- 200 MiB 正文流过，服务端峰值常驻内存 **2 MiB**，与正文大小无关 → 确为增量解析。
- SHA-256 `fb3a4ee0…242be99` 与 `python3 -c "hashlib.sha256(b'A'*209715200)"` 结果一致。

## 7. 开发过程中发现并修复的两个真实缺陷（如实记录）

1. **假边界候选会丢字节**：正文扫描遇到“前缀匹配但后缀不符”的近似边界（如
   `\r\n--B-x`）后只推进内部扫描下标、不立即确认正文；若随后因数据不足提前返回，
   候选之前的正文字节会滞留。修复为确认到候选首字节后再继续扫描。
   （由随机属性测试设计 + 手工复现定位。）
2. **“需要更多数据”与 cut=0 混淆**：当缓冲区恰好是一条完整分隔符、但其后 2 个
   分类字节尚未到达时，返回 0 会让 `feed` 外层空转死循环。修复为返回 NEED_MORE。

另修复了若干**测试自身**的问题（均非解析器缺陷）：测试 helper 硬编码 boundary、
把“Body 事件切分粒度”误当不变量比较、RFC 上合法的“关闭边界后无 CRLF”被误判为截断、
total 上限先于 part 上限触发导致用例没测到目标分支。

## 8. 未通过项 / 已知限制

- 无未通过的测试或验收项。
- 服务端按设计拒绝（非缺陷）：preamble、epilogue、chunked 请求体、keep-alive、
  非 `multipart/form-data` 类型；这是“明确子集”的有意取舍。
