# 运行记录（RUNLOG）

日期：2026-09-25
平台：Linux 6.8.0-90-generic (x86_64)
工具链：rustc 1.98.1 / cargo 1.98.1（edition 2021），零外部依赖

## 环境备注（如实记录）

会话开始时系统默认的 rustup 工具链损坏（`missing manifest in
toolchain 'stable-x86_64-unknown-linux-gnu'`），且通过 rustup 在线重装
多次因下载被同机其他并行安装进程争用/中断而失败（`bad checksum for
cached download`、component rename 失败）。最终使用机器上已存在的完整
工具链直接构建：

```bash
export PATH=$HOME/rustup-fresh/toolchains/stable-x86_64-unknown-linux-gnu/bin:$PATH
```

项目本身不含任何网络依赖，`cargo build` 全程离线、不访问 crates.io。

## 实际执行的命令与结果

### 1. 格式化

```text
$ cargo fmt
$ cargo fmt --check
fmt-ok
```

### 2. 静态检查（警告即错误）

```text
$ cargo clippy --all-targets -- -D warnings
    Finished `dev` profile [unoptimized + debuginfo]
```

无任何 warning/error。

### 3. 构建

```text
$ cargo build
    Finished `dev` profile
$ cargo build --release
    Finished `release` profile [optimized]
```

产物：`target/release/rbitmap`（约 611 KiB 单文件，无动态库依赖）。

### 4. 自动化测试

```text
$ cargo test
running 5 tests  (src/json.rs 内联单元测试)            ... ok
running 2 tests  (src/base64.rs 内联单元测试)          ... ok
tests/api_test.rs     ... 8 passed; 0 failed
tests/bitmap_test.rs  ... 18 passed; 0 failed
tests/codec_test.rs   ... 6 passed; 0 failed
```

合计 **37 个测试全部通过，0 失败**。其中关键验收项：

- `random_sets_match_btree_oracle`：200 轮随机，4 种密度体制（全 32
  位空间、单容器、稠密簇、阈值跨越），并/交/差与成员判定逐项对照
  `std::collections::BTreeSet<u32>`，且每轮同时校验二进制往返。
- `container_switching_preserves_semantics`：4096 阈值双向切换
  （4096 数组 → 4097 位图 → 删除回 4096 数组），逐值验证；空容器消失。
- `boundary_values`：0、1、65535、65536、u32::MAX 等边界，升序迭代。
- `extremely_dense_sets`：4 个完整容器（262144 值）与偶值集合运算。
- `extremely_sparse_sets`：64 个随机孤立值散布在全 u32 范围。
- `incremental_updates_match_oracle`：20000 次随机插入/删除交错对照。
- 恶意输入：每个截断点（逐字节）、坏 magic/version/flags、尾部垃圾、
  超大容器数（70000>65536）、超大数组声明（10⁹）、重复/逆序键、重复
  /逆序数组值、未知 tag、非规范 varint、输出预算超限——全部返回
  `Err`，无 panic。
- `encode_stream_matches_encode` / `decode_stream_matches_decode`：
  流式与缓冲路径字节级一致。

### 5. 端到端（CLI / JSON 入口）

```text
$ ./target/release/rbitmap examples/build.json
{"ok":true,"result":{"set":"UkJNMQEAAwAAAQMBAAIAAwABAAEBcBH//wEB//8=",
 "stats":{"cardinality":5,"containers":3,"array_containers":3,
 "bitmap_containers":0,"encoded_bytes":29}}}

$ ./target/release/rbitmap --batch examples/ops.jsonl   # 6 行请求，见下
$ ./examples/demo.sh ./target/release/rbitmap           # 全操作串联演示
```

并/交/差实测结果（A={1,2,3,70000,4294967295}，B={3,4,5,65536,4294967295}）：

| 操作 | 结果基数 | decode 列出的值 |
|------|---------|-----------------|
| union | 8 | 1,2,3,4,5,65536,70000,4294967295 |
| intersect | 2 | 3,4294967295 |
| difference A∖B | 3 | 1,2,70000 |

成员判定：70000→true，70001→false；insert/remove 返回 `changed` 正确。

恶意容器长度经 JSON 入口的真实输出：

```text
$ HOSTILE=$(printf 'RBM1\x01\x00\xf0\xa2\x04' | base64 -w0)
$ echo "{\"op\":\"decode\",\"set\":\"$HOSTILE\"}" | ./target/release/rbitmap
{"ok":false,"error":"declared container count length 70000 exceeds limit 65536"}
```

进程不崩溃，退出码 0（协议错误作为数据返回）。

## 未通过项 / 已知限制（如实记录）

- **未生成覆盖率百分比报告。** 规则建议 80% 覆盖率并用
  `cargo-llvm-cov`，但该工具未安装、`llvm-tools` 组件无法在当前受限
  网络下补齐，故未产出数字报告。作为补偿，测试按"行为面"组织：随机
  oracle、容器双向切换、极密/极稀、全部边界值、逐字节截断与各类恶意
  长度均有专门用例，核心模块（container/bitmap/codec/api/json/base64）
  的正常与错误路径都被执行到。
- 在线 `rustup toolchain install` 在本机不稳定（并行安装争用），不
  影响项目：最终使用预装的 1.98.1 完成全部构建与测试。
- JSON 数字以 f64 承载；`values` 经专门的 u32 校验（拒绝负数、小数、
  >2³²−1），因此集合值域严格限定 u32。
- 仅实现了需求要求的数组/位图两类容器（RBM1 v1）；Roaring 官方格式
  的 run-container 标签未使用（tag 空间已在 FORMAT.md 预留语义说明，
  未知 tag 一律安全拒绝）。
