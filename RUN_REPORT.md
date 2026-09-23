# 运行报告（RUN_REPORT）

本文件如实记录本项目的实际构建、测试与端到端运行情况，包括开发过程中
**实际遇到并修复的缺陷**，以及最终状态。日期：2026-09-23。

## 1. 环境

| 项 | 值 |
|---|---|
| OS | Linux 6.8.0-90-generic (x86_64) |
| Rust | rustc 1.98.1 / cargo 1.98.1（stable） |
| 依赖 | 无第三方 crate（仅标准库） |
| 验证工具 | curl 8.5.0、bash、python3（仅 demo/数据生成用） |

## 2. 实际执行的命令与结果

### 2.1 构建

```bash
cargo build            # debug，通过，0 警告
cargo build --release  # 通过
cargo clippy --all-targets -- -D warnings   # 通过，0 警告
```

### 2.2 自动化测试（`cargo test`，最终结果）

```text
unittests src/lib.rs ...      test result: ok. 20 passed; 0 failed
unittests src/main.rs ...     test result: ok.  0 passed; 0 failed
tests/corruption.rs ...       test result: ok. 12 passed; 0 failed
tests/edge_cases.rs ...       test result: ok.  4 passed; 0 failed
tests/file_build.rs ...       test result: ok.  5 passed; 0 failed
tests/http_server.rs ...      test result: ok.  3 passed; 0 failed
tests/io_faults.rs ...        test result: ok.  8 passed; 0 failed
tests/random_diff.rs ...      test result: ok.  2 passed; 0 failed
                                                （其中含 40 个随机种子 × BTreeMap 差分）
```

**合计 54 个测试，0 失败、0 忽略。**

测试覆盖对应验收要求：

* **随机键对照有序映射**：`tests/random_diff.rs` —— 40 个种子，每轮 1–400 个
  随机长度随机字节键，点查（含命中/未命中探针）、全表扫描、随机上下界
  （含/不含四种组合）扫描、limit，全部与 `BTreeMap` 逐一对照；
  另有一个 2000 键、50+ 块的多块大表用例。
* **空键**：`edge_cases::empty_key_point_get_and_ordering`（空键作为最小键，
  含/不含下界扫描），HTTP 层 `get?key=` 实测。
* **长公共前缀**：`edge_cases::long_common_prefix_still_gets_and_scans`
  （约 680 字节共享前缀 × 60 键，断言文件小于朴素键字节总和，点查/扫描一致）。
* **跨块扫描**：`edge_cases::cross_block_scan_with_narrow_bounds`
  （500 键、20+ 块；边界命中块边界/重启点/键间隙；含空区间、跨块 limit）。
* **损坏重启偏移**：`corruption.rs` 三个用例——
  `restart[0] != 0`、restart 指向自身数组、`restart[1] == restart[0]`；
  篡改后**重算 CRC** 以确保错误走到结构校验层而不是被校验和提前拦截。
* **故障注入**：`tests/io_faults.rs` —— append 失败（早/晚）、sync 失败、
  恒定 3 字节短写（产物字节一致）、7 字节碎片读、注入 read 失败。

### 2.3 真实 HTTP 端到端运行（`scripts/demo.sh`）

在真实 TCP 端口上启动 release 二进制，用 curl 完成
健康检查 → 建表（空键 + 40 个长前缀键）→ 重名 409 → 点查（命中/未命中）→
扫描 → 严格校验 → **翻转磁盘文件一个字节后校验返回 400 + checksum mismatch** →
删除 → 列表空。最终输出 `demo finished OK`。

关键实测响应：

```text
建表   {"ok":true,"table":"demo","bytes":1562,"entries":41,"data_blocks":5}
重名   HTTP 409
空键   {"ok":true,"found":true,"key":"","value":"726f6f74"}
未命中 {"ok":true,"found":false,"key":"ffff"}
校验   {"ok":true,"valid":true,"file_size":1562,"total_entries":41,...5 个 data block}
损坏后 HTTP 400
       {"ok":false,"error_kind":"corruption",
        "error":"corruption: block checksum mismatch at offset 550 (stored 0xbc0dfd0e, computed 0x426411a1)"}
删除   {"ok":true,"deleted":"demo"}
```

> 备注：本机 `127.0.0.1:8099`、`8088` 被其他进程占用，demo 实际使用
> `ADDR=127.0.0.1:18099`（另用 18077 做文档核对）、临时目录 `/tmp/psst-demo-run*` 运行；
> 不影响结论。默认监听地址仍为 `127.0.0.1:8088`。

## 3. 开发过程中实际发现并修复的缺陷（如实记录）

这些问题都由**先写的测试或实际运行**暴露，随后修复，最终全部通过：

1. **重启点计数逻辑错误（严重，影响正确性）**。初版用“距上次重启计数 ≥
   interval 才建重启点”，且每次 flush 后清零，导致 `restart_interval > 1` 时
   每个块的第一个条目不落在偏移 0，触发 `first restart point must be at
   offset 0`。修复为按条目总数取模：`num_entries % interval == 0`，
   与 LevelDB 语义一致。
2. **连续超大条目刷盘会丢失数据块（严重，影响正确性）**。单个条目大于块目标
   时会连续 flush，初版的“待写索引项”被后一次 flush 覆盖，导致中间数据块
   句柄丢失、`validate` 报 “data blocks do not tightly precede the metaindex
   block”（长前缀用例实测暴露）。修复为 LevelDB 式**惰性分隔符**：
   在下一次 `add` 已知下一块首键时补索引项，flush 内对连续超大条目做正确衔接。
3. **CRC32C 测试向量取错约定**。实现采用 LevelDB 约定（初值 0、无末尾异或），
   初版测试误用了经典 RFC 3720 向量 `0xe3069283`（初末值均异或 0xffffffff）。
   实现不变，测试改为正确向量 `0x58e3fa20` 并在注释中写明两种约定的差异。
4. **空表被误拒**。初版块解析要求至少 1 个重启点，使 0 条目表无法打开；
   修正为“空块恰好是 4 字节的 `num_restarts=0`”，并增加空表用例。
5. **文件重名构建静默覆盖**。Linux `rename(2)` 会覆盖已存在目标，初版未显式
   检查导致重复 PUT 返回 201（HTTP 端到端测试暴露，实测复现）。
   增加重命名前的存在性检查，返回 `AlreadyExists`（HTTP 409）。
6. **`FaultyWriter` 短写语义不真实**。初版“声称写了 n 字节但整块都已落盘”，
   修正为只把前 n 字节交给底层设备，真实模拟部分写。
7. 若干测试假设/笔误：注入故障实际落在 `add` 而非 `finish`（改为断言“无论
   落在哪一步都为 Io 错误且产物不可用”）；长前缀用例块目标过小（每块一条
   无法压缩）改为 8 KiB；篡改 metaindex 句柄在 open 阶段不会被读到（open
   只急读 footer+index），改为 index 句柄在 open 失败、metaindex 句柄在
   validate 失败两个用例；路径穿越测试移除了对无关的预存 `/tmp/x` 的假设。
8. 服务层初版手写 JSON 时输出枚举与解析树类型混用、`ScanIter` 自引用等
   编译期问题，均在首个编译周期内修复。

## 4. 最终状态与未通过项

* `cargo build` / `cargo build --release`：通过，零警告。
* `cargo clippy --all-targets -- -D warnings`：通过，零警告。
* `cargo test`：**54/54 通过**。
* 真实 HTTP 端到端 demo：通过（含损坏检测实测）。
* **未通过项：无。** 上述缺陷均已修复并有回归测试锁定。

## 5. 范围说明（不做的部分）

本项目交付的是**只读有序表文件 + 构建器 + 本地验证 HTTP 入口**，不含
WAL/MemTable、compaction、Bloom、多表合并与鉴权；HTTP 服务按线程处理连接，
定位为本地验证工具而非生产服务器。无前端。
