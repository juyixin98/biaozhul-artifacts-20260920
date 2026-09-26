# 测试与运行记录

本文档如实记录交付前实际执行的命令与结果。执行日期：2026-09-25，平台：Linux x86_64。

## 环境说明（如实记录）

- 机器初始**没有 Rust 工具链**。通过 rustup 安装时，同机多个并发会话争抢 `~/.rustup` 导致工具链损坏（`missing manifest in toolchain`），隔离安装（独立 `RUSTUP_HOME`）又两次被系统 SIGKILL（内存紧张）、一次前台下载超时。
- 最终改用系统包管理器：`sudo apt-get install -y rustc cargo`，得到 **rustc/cargo 1.75.0**。项目代码与依赖（serde 1.0.229、serde_json 1.0.151、base64 0.22.1）均与该版本兼容。
- `cargo clippy` 在该环境不可用（未安装组件），未执行；以 `cargo build` 无警告代替。

## 构建

```
$ cargo build
    Finished dev [unoptimized + debuginfo] target(s) in 1m 50s
$ cargo build --release
    Finished release [optimized] target(s) in 13.03s
```

均无警告。

## 自动化测试

```
$ cargo test
```

结果：**42 个测试全部通过，0 失败**。

| 测试套件 | 数量 | 结果 | 覆盖内容 |
|---|---:|---|---|
| 单元测试（src） | 13 | 全部通过 | 位读写混合位宽/边界 0 与 64/超宽拒绝/EOF；zigzag 极值往返；块级：位宽 0 常量块、负差值、极端异常值逃逸、i64 全范围差值、强制位宽 0..=64；流级：空流、多块索引定位 |
| tests/roundtrip.rs | 10 | 全部通过 | 空/单值/i64::MIN/MAX、负值混合、强制位宽 0..=64 往返、极端异常点（i64::MIN/MAX 等）往返且不抬高位宽、全范围随机、各数量级随机、稀疏异常点、块大小边界、单块定位与切片一致 |
| tests/corrupt.rs | 14 | 全部通过 | 坏 magic、逐长度截断不 panic、流头逐字节篡改不 panic、位宽 65/100/200/255 拒绝（无移位溢出）、value_count=u32::MAX 不触发巨额分配、total_values=u64::MAX 被限制拦截、异常计数越界、packed_len 不符、索引偏移越界、尾部多余字节、非法版本/flags、随机垃圾输入不 panic、块号越界、输出长度限制强制 |
| tests/cli.rs | 5 | 全部通过 | JSON 入口 encode→decode 往返、i64 极值往返、inspect 与 decode_block、坏请求/坏 base64/垃圾数据报错、limits 强制 |

### 开发过程中出现并已修复的问题（如实记录）

1. `tests/roundtrip.rs` 数组迭代模式 `for (i, &outlier) in [...]` 在 edition 2021 下类型不匹配（E0308）——已改为 `for (i, outlier)`。
2. `src/block.rs` 测试中 `header.bit_width`(u8) 与 `w`(u32) 直接比较类型不匹配——已改为 `u32::from(header.bit_width)`。
3. 初版 `every_bit_width` 测试构造的数据多数值为 0，分位数策略会选位宽 0 而非目标位宽——已修正测试数据构造（多数值携带目标位宽差值，w=1 用差值 -1）。

修复后无未通过项。

## JSON 控制入口实测

```
$ ./target/release/bitpack-ctl examples/requests/encode.json
{"data":"QlBCMQEAAAAIAAAAAgAAAAoAAAAAAAAApAAAAAAAAAAFAAAAAAAAAEAIAAAAAAAAAEAAAAAAAAAAAAAAAAAAAAMAAAAAAAAAFwAAAAAAAAC+AAAAAAAAAAMAAAAAAAAAAAAAAAAAAACJhB4AAAAAAPT/////////AAAAAAAAAIBAAgAAAAEAAAAIAAAAAAAAAAAAAAAAAAABAAAAKgAAAAAAAAAgAAAAAAAAAAAAAAAAAAAAeAAAAAAAAAAIAAAAAAAAAA==","ok":true,"stats":{"block_count":2,"block_size":8,"byte_len":196,"total_values":10}}

$ ./target/release/bitpack-ctl examples/requests/decode.json
{"ok":true,"values":[5,3,-7,100,3,5,-1000000,9223372036854775807,-9223372036854775808,42]}

$ ./target/release/bitpack-ctl examples/requests/inspect.json
{"header":{"block_count":2,"block_size":8,"index_offset":164,"total_values":10},"index":[{"block_offset":32,"first_value_index":0},{"block_offset":120,"first_value_index":8}],"ok":true}

$ ./target/release/bitpack-ctl examples/requests/decode_block.json
{"block":1,"ok":true,"values":[-9223372036854775808,42]}
```

异常输入（均返回 `{"ok":false,...}` 且退出码 1，进程不崩溃）：

```
$ echo '{"op":"decode","data":"QUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUE="}' | ./target/release/bitpack-ctl
{"error":"invalid magic bytes","ok":false}        # exit=1
$ echo '{"op":"decode","data":"QlBCMQE="}' | ./target/release/bitpack-ctl
{"error":"unexpected end of input","ok":false}   # exit=1
$ echo '{"op":"boom"}' | ./target/release/bitpack-ctl
{"error":"bad request: unknown op \"boom\"","ok":false}  # exit=1
```

## 验收点对照

| 验收要求 | 结果 |
|---|---|
| 位宽 0 与 64 边界往返 | 通过（`forced_bit_widths_0_through_64`、`constant_values_use_bit_width_zero` 等） |
| 负值往返 | 通过（`negative_and_mixed_signs`、`negative_deltas`） |
| 极端异常点（i64::MIN/MAX 等）往返 | 通过（`extreme_outliers_escape_and_roundtrip`、`full_range_deltas_still_roundtrip`、`extremes_roundtrip_via_json`） |
| 坏头不导致移位溢出 | 通过（位宽 >64 在移位前拒绝：`bit_width_above_64_is_rejected_without_shift_overflow`） |
| 坏头不导致越界分配 | 通过（所有计数在分配前校验：`huge_value_count_cannot_force_huge_allocation` 等） |
| 索引定位单块 | 通过（`single_block_access_matches_slice`、`decode_block` CLI） |
| 明确字节序 | 全格式小端，flags=0 固定，FORMAT.md §1/§2 明示 |
| 内存与解码输出长度限制 | `Limits` 四项限制，解码前强制，CLI 可配 |
