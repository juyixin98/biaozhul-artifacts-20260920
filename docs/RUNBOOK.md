# 运行记录（RUNBOOK）

本文件记录项目实际执行的命令、关键结果与过程中遇到的问题，均为真实输出
（机器：Linux x86_64，Rust 1.98.1 stable）。

## 0. 环境准备（实际遇到的问题）

机器预装的 rustup 处于损坏状态（`no installed toolchains`）。按常规
`rustup toolchain install stable` 反复失败，原因是该机器上**有其他并行会话
同时安装 Rust**，共用 `~/.rustup/downloads`，多进程互相 rename/删除对方的
`.partial` 文件（报 `could not rename ... No such file or directory`、
`bad checksum for cached download`、`premature eof`）。

为不干扰其他会话，本项目使用**隔离目录 + 国内镜像**安装，后续所有 cargo
命令都带这三个环境变量：

```bash
export RUSTUP_HOME=/tmp/cdict-rustup
export CARGO_HOME=/tmp/cdict-cargo
export RUSTUP_DIST_SERVER=https://rsproxy.cn
rustup toolchain install stable --profile minimal --no-self-update   # RC=0
rustup component add rustfmt clippy                                  # RC=0
rustc --version   # rustc 1.98.1 (48a229cea 2026-09-01)
cargo --version   # cargo 1.98.1 (797e8a9bc 2026-08-05)
```

> 在单一用户、无并发安装的正常环境下，直接 `rustup toolchain install stable`
> 即可，无需上述隔离变量。

## 1. 构建

```text
$ cargo build
    Compiling cdict v0.1.0
    Finished `dev` profile [unoptimized + debuginfo]

$ cargo build --release
    Finished `release` profile [optimized]
```

零编译警告。

## 2. 静态检查

```text
$ cargo fmt --all -- --check     # 先发现格式差异，执行 cargo fmt --all 后干净
$ cargo clippy --all-targets -- -D warnings
    Finished `dev` profile        # 无任何 warning/error
```

修复过程中处理了 3 个 clippy 项：1 个 `type_complexity`（提取 `LimitField`
类型别名）、2 个 `unnecessary_cast`（JSON 代理对计算里的冗余 `as u32`）。

## 3. 自动化测试

```text
$ cargo test --release
```

| 测试目标 | 数量 | 结果 |
|---|---:|---|
| 库单元测试（varint / segment / json） | 18 | 全部通过 |
| tests/control_plane.rs | 5 | 全部通过 |
| tests/end_to_end.rs | 15 | 全部通过 |
| tests/merge_property.rs | 5 | 全部通过 |
| doc-test（lib.rs 示例） | 1 | 通过 |
| **合计** | **44** | **0 失败** |

覆盖的验收点：

* 空列（0 段）、空串、全 NULL（空字典）、空串≠NULL；
* Unicode：2 字节（é）、3 字节（中/€）、4 字节（🙂/🦀）及 JSON 代理对；
* 大基数退化：5000 个全异值按 137 行/段，逐行一致；
* 多种段大小（1、2、3、7、100、1000、1001 行/段）下逐行一致；
* 多流分段合并：本地 id 不同的同值合并后逐行值完全一致、获得相同全局 id；
* 字典插入顺序相反的两流各自解码正确（顺序不影响解码）；
* 截断、错误魔数、错误版本、悬空 id、段数/字典/输出长度等限制被拒绝；
* 流式 Encoder/Decoder（2000 行、128 行/段）逐行一致。

## 4. CLI 端到端（真实输出）

```text
$ target/release/cdict examples/encode.request.json
{"ok":true,"op":"encode","rows":8,"segments":3}

$ target/release/cdict examples/decode.request.json   # 再 cat 输出文件
["apple","banana",null,"apple","","樱桃",null,"banana"]

$ target/release/cdict examples/encode2.request.json
{"ok":true,"op":"encode","rows":4,"segments":2}

$ target/release/cdict examples/merge.request.json
{"ok":true,"op":"merge","rows":12,"input_streams":2,"input_segments":5,
 "output_segments":3,"global_dict_entries":5}

$ target/release/cdict examples/inspect.request.json
{"ok":true,"op":"inspect","rows":12,"segments":3,"null_rows":3,
 "string_bytes":43,"distinct_values":5}
```

合并后解码（rows.cdc + rows2.cdc 拼接顺序，逐行一致）：

```json
["apple","banana",null,"apple","","樱桃",null,"banana","banana","date",null,"apple"]
```

二进制头部（`xxd out/rows.cdc`）可看到 `CDCT\x01` 与 `SEGM` 魔数、varint
计数、字典长度前缀：

```text
00000000: 4344 4354 0153 4547 4d02 0305 6170 706c  CDCT.SEGM...appl
00000010: 6506 6261 6e61 6e61 0102 0053 4547 4d03  e.banana...SEGM.
```

## 5. 边界与错误处理（真实输出）

```text
空列 encode/decode      => {"rows":0,"segments":0} 与 []
全 NULL + 空串 inspect   => {"rows":4,"null_rows":3,"distinct_values":1}
Unicode 往返            => ["中文","🙂","é","🦀",null] 原样
超 max_dict_entries     => resource limit exceeded ... 退出码 1
随机垃圾（错误魔数）     => invalid data: bad file magic ... 退出码 1
未知 op                  => invalid data: unknown op "nope" 退出码 1
非法 JSON 请求           => invalid data: expected string key ... 退出码 1
```

## 6. 大规模实测（流式、合并、大基数）

单流 1,000,000 行（约 1/7 为 NULL，其余 1000 个不同值），50,000 行/段：

```text
encode  => {"rows":1000000,"segments":20}
inspect => {"rows":1000000,"segments":20,"null_rows":142858,
            "string_bytes":5048567,"distinct_values":1000}
decode  后 cmp 原始文件  => IDENTICAL: original == decoded (1,000,000 rows)
体积    => JSON 8,334,284 字节 -> CDCT 1,885,907 字节（约 22.6%）
```

再合并第二流（300,000 行、7,000 行/段、500 个不同值域）：

```text
merge => {"rows":1300000,"input_streams":2,"input_segments":63,
          "output_segments":33,"global_dict_entries":1500}
decode 后与两流拼接期望 cmp => IDENTICAL after merge: 1,300,000 rows
```

大基数退化 200,000 个全异值（10,000 行/段）：

```text
inspect => distinct_values=200000（每段字典近似全异）
decode cmp 原始 => IDENTICAL high-card 200k rows
体积     JSON 4,200,001 -> CDCT 4,197,625（退化时几乎不压缩，仍正确）
```

限制在规模下真实生效：

```text
max_decoded_string_bytes=100000 解码 => decoded output exceeds 100000 string bytes（退出码 1）
max_dict_entries=50000 编码 200k 全异值 => dictionary exceeds 50000 entries（退出码 1）
```

## 7. 已知限制 / 未做项（如实说明）

* **无前端、无网络服务**：仅库 + 本地 CLI + 测试，符合需求。
* **stdin 只有一个**：CLI 参数为 `-`（请求走 stdin）时，数据若也要用
  `input:"-"` 会与请求争用 stdin。需要管道传数据时请把**请求放在文件
  参数**、数据走 stdin（README 已明确）。这是单进程单 stdin 的固有限制，
  非编解码缺陷；数据/请求任一走文件则无此问题。
* 控制面为简便起见把单个输入文件读入内存（上限 1 GiB，
  `MAX_CONTROL_INPUT_BYTES`）；底层编解码库本身是流式的，逐段内存有界。
* 未引入通用压缩层（gzip/zstd 等）：格式刻意只做字典编码，详见
  `docs/FORMAT.md` §7。
* 因隔离工具链装在 `/tmp/cdict-cargo`（非默认 `~/.cargo`），复现时需带上
  §0 的环境变量，或在正常环境直接用默认 stable。

## 8. 一键复现

```bash
scripts/verify.sh            # fmt + clippy + 全部测试 + release 构建 + demo
scripts/verify.sh --no-demo  # 仅静态检查与测试
```
