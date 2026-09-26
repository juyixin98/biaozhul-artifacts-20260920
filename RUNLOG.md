# 运行记录（RUNLOG）

记录日期：2026-09-25。所有命令在 `/home/admin/Downloads/biaozhul/opp161/a` 下执行。
结论先行：**全部 27 个自动化测试通过，无未通过项**；CLI 演示全部符合预期。

## 环境准备（如实记录遇到的问题）

本机最初没有 Rust 工具链，安装过程中遇到两类真实问题：

1. 官方源 `static.rust-lang.org` 下载组件时反复失败
   （`.partial` 文件在 rename 前消失，报 `No such file or directory`）。
2. 本机被多个并发会话共享，`~/.rustup` 下的工具链被其他会话的
   安装/回滚操作删除，出现 `missing manifest in toolchain` 与 shim 卡死。

解决：改用镜像 `https://rsproxy.cn`，并将工具链安装到项目私有目录
（不依赖共享的 `~/.rustup`）：

```bash
curl -sSfL -o /tmp/rust-stable.tar.gz \
  https://rsproxy.cn/dist/rust-1.98.1-x86_64-unknown-linux-gnu.tar.gz
tar -xzf /tmp/rust-stable.tar.gz -C .toolchain
.toolchain/rust-1.98.1-x86_64-unknown-linux-gnu/install.sh \
  --prefix=$PWD/.toolchain/install \
  --components=rustc,cargo,rust-std-x86_64-unknown-linux-gnu
```

验证：

```
$ .toolchain/install/bin/rustc --version
rustc 1.98.1 (48a229cea 2026-09-01)
$ .toolchain/install/bin/cargo --version
cargo 1.98.1 (797e8a9bc 2026-08-05)
```

后续命令均以 `export PATH=$PWD/.toolchain/install/bin:$PATH CARGO_HOME=$PWD/.cargo-home`
开头（`.toolchain/` 与 `.cargo-home/` 已加入 `.gitignore`，体积约 577 MB，
仅用于本地构建，可随时删除后用任何 Rust 1.7x+ 工具链重新构建）。

## 构建

```
$ cargo build
   Compiling canohuff v0.1.0
    Finished `dev` profile [unoptimized + debuginfo] target(s) in 1.22s
$ cargo build --release
    Finished `release` profile [optimized] target(s) in 1.46s
```

无编译警告。

## 自动化测试

```
$ cargo test
     Running tests/cli.rs
test result: ok. 4 passed; 0 failed
     Running tests/corruption.rs
test result: ok. 14 passed; 0 failed
     Running tests/roundtrip.rs
test result: ok. 9 passed; 0 failed
```

合计 **27 passed / 0 failed**。覆盖：

- 往返：空输入、单字节、单符号重复、双符号、256 符号等频率、
  1 B–1 MiB 多尺寸伪随机数据、确定性（同输入同输出）、倾斜数据压缩
- 损坏/伪造：头部与位流截断、坏 magic、过度订阅码表、不完整码表
  （permit/reject 两策略）、非法码长（0/33/255）、重复符号、
  伪造声明长度（u64::MAX 触发输出上限、改小触发尾随数据）、
  尾随垃圾字节、非零填充位、输出上限、空表带数据、未定义码字
- CLI 端到端：JSON 请求压缩→解压往返、未知 op、畸形 JSON、截断文件

## CLI 实际运行（release 二进制）

### 随机数据 1 MiB（不可压缩，如实膨胀）

```
$ echo '{"op":"compress","input":"/tmp/chf-rand.bin","output":"/tmp/chf-rand.chf"}' | ./target/release/chf -
{"ok":true,"op":"compress","input_bytes":1048576,"output_bytes":1049102,"ratio":1.0005,"elapsed_ms":19}
$ echo '{"op":"decompress","input":"/tmp/chf-rand.chf","output":"/tmp/chf-rand.out"}' | ./target/release/chf -
{"ok":true,"op":"decompress","input_bytes":1049102,"output_bytes":1048576,"ratio":0.9995,"elapsed_ms":19}
$ cmp /tmp/chf-rand.bin /tmp/chf-rand.out && echo IDENTICAL
IDENTICAL
```

随机数据 ratio = 1.0005，**变大了约 0.05%**（码表头 526 字节 + 熵极限），
符合"不保证所有输入变小"的声明。

### 文本/源码数据 54 KiB

```
compress:   {"ok":true,...,"input_bytes":54728,"output_bytes":36202,"ratio":0.6615,...}
decompress: {"ok":true,...,"input_bytes":36202,"output_bytes":54728,...}
cmp 校验: IDENTICAL
```

### 空输入

```
compress:   {"ok":true,...,"input_bytes":0,"output_bytes":14,"ratio":null,...}
decompress: {"ok":true,...,"input_bytes":14,"output_bytes":0,...}
cmp 校验: IDENTICAL（压缩产物恰为 14 字节空头部）
```

### 截断位流（失败路径）

```
$ head -c 30 /tmp/chf-text.chf > /tmp/chf-trunc.chf
$ echo '{"op":"decompress","input":"/tmp/chf-trunc.chf","output":"/tmp/chf-trunc.out"}' | ./target/release/chf -
{"ok":false,"op":"decompress","error":"truncated stream: unexpected end of input"}
exit=1
```

### 示例请求文件（examples/requests/）

```
$ head -c 200000 /dev/urandom > /tmp/chf-demo-input.bin
$ ./target/release/chf examples/requests/compress.json
{"ok":true,"op":"compress","input_bytes":200000,"output_bytes":200526,"ratio":1.0026,"elapsed_ms":3}
$ ./target/release/chf examples/requests/decompress.json
{"ok":true,"op":"decompress","input_bytes":200526,"output_bytes":200000,"ratio":0.9974,"elapsed_ms":5}
cmp 校验: IDENTICAL
```

## 未通过项

无。所有测试与演示均通过；唯一"失败"是环境安装阶段的工具链下载问题
（见上文），与项目代码无关，已通过镜像 + 私有目录解决。
