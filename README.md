# inc-utf8 — 增量 UTF-8 验证与流式编解码

纯后端 Rust 项目：一个**零依赖**的流式（增量）UTF-8 验证/解码库，外加一个
newline-delimited JSON(NDJSON）控制入口 `utf8ctl`。核心编解码算法与 JSON
解析器均为自行实现，不依赖任何第三方 crate（包括 `serde`)。

- 跨块（cross-chunk）UTF-8 验证与码点解码：输入可以按任意字节边界切分喂入；
- 严格校验：**拒绝过长编码（overlong)、UTF-16 代理项（surrogate)、超出
  U+10FFFF 的值**以及一切非法前导/续字节；
- 流结束时若存在未完成序列，`finish()` 报错并给出该序列在**原始字节流中的
  绝对偏移**;
- 资源限制：可配置总输入字节上限与总输出码点上限，解码器本身状态为 O(1);
- 错误恢复策略显式可选，**绝不静默替换损坏内容**（永远不会自动产出
  U+FFFD 替换字符）。

## 项目结构

```
Cargo.toml               # 无依赖
src/
  lib.rs                 # 库入口与公共导出
  decoder.rs             # 增量 UTF-8 验证/解码状态机（核心）
  encoder.rs             # 码点 -> UTF-8 编码器（核心）
  error.rs               # 错误类型（含绝对字节偏移）
  hex.rs                 # 控制协议用的 hex 编解码
  json.rs                # 最小 JSON 解析/序列化（自实现）
  bin/utf8ctl.rs         # JSON 控制入口（NDJSON, stdin/stdout）
tests/
  split_points.rs        # 所有切分点测试（2 块全切分、3 块切分对、逐字节）
  invalid_sequences.rs   # 非法字节组合：过长/代理项/超范围/截断等
  roundtrip.rs           # 全码点空间编码-解码往返（含逐字节喂入）
  recovery.rs            # 错误恢复策略（不静默替换）
  limits.rs              # 输入/输出资源限制
  cli.rs                 # utf8ctl 端到端 JSON 协议测试
examples/requests/       # 请求样例（可直接管道给 utf8ctl)
```

## 构建与测试

```bash
cargo build
cargo test
```

本机实际使用的是系统 Rust 1.75.0(`/usr/bin/cargo`)，因 rustup 在线安装
受网络影响失败，实际命令为（离线、无依赖，不需要网络）:

```bash
RUSTC=/usr/bin/rustc RUSTDOC=/usr/bin/rustdoc /usr/bin/cargo test --offline
```

运行请求样例：

```bash
cargo build
./target/debug/utf8ctl < examples/requests/01_decode_simple.jsonl
```

## 流式解码格式说明（状态机）

解码器按 Unicode 标准 Table 3-7 的**严格续字节范围**校验每个序列，状态
仅为「还需几个续字节 + 已累积码点 + 下一字节允许区间」，跨块最多携带
3 字节状态：

| 前导字节      | 序列长度 | 第 2 字节允许范围 | 后续字节 | 拒绝目标            |
|---------------|----------|-------------------|----------|---------------------|
| `00..=7F`     | 1        | —                 | —        | —                   |
| `80..=BF`     | —        | —                 | —        | 孤立续字节，报错    |
| `C0..=C1`     | —        | —                 | —        | 过长编码，报错      |
| `C2..=DF`     | 2        | `80..=BF`         | —        | —                   |
| `E0`          | 3        | `A0..=BF`         | `80..=BF`| 过长编码            |
| `E1..=EC`     | 3        | `80..=BF`         | `80..=BF`| —                   |
| `ED`          | 3        | `80..=9F`         | `80..=BF`| 代理项 U+D800–DFFF  |
| `EE..=EF`     | 3        | `80..=BF`         | `80..=BF`| —                   |
| `F0`          | 4        | `90..=BF`         | `80..=BF`| 过长编码            |
| `F1..=F3`     | 4        | `80..=BF`         | `80..=BF`| —                   |
| `F4`          | 4        | `80..=8F`         | `80..=BF`| 超范围 (>U+10FFFF)  |
| `F5..=FF`     | —        | —                 | —        | 非法前导字节，报错  |

编码器只做最短编码，且只接受 Unicode 标量值（拒绝代理项与 >U+10FFFF),
因此「编码→解码」往返对全部 1 114 112 个标量值恒等（测试覆盖）。

### 错误类型（`ErrorKind`)

| kind                      | 含义                                       |
|---------------------------|--------------------------------------------|
| `unexpected_continuation` | 前导位置出现续字节                          |
| `invalid_lead_byte`       | `F5..=FF` 等永不可能合法的前导字节          |
| `invalid_continuation`    | 续字节不在 `80..=BF` 范围内                 |
| `overlong_encoding`       | 过长编码（`C0/C1`、`E0 80..`、`F0 80..` 等)|
| `surrogate`               | 编码了 U+D800..=U+DFFF                     |
| `out_of_range`            | 编码了 >U+10FFFF 的值                       |
| `truncated_sequence`      | `finish()` 时序列未完成                     |
| `input_limit_exceeded`    | 超过输入字节上限                            |
| `output_limit_exceeded`   | 超过输出码点上限                            |

每个错误都携带 `offset`（触发错误的字节的绝对偏移）与 `sequence_start`
（所属序列首字节的绝对偏移），偏移跨 `feed` 调用累计，定位的是**原始
字节流**中的位置。

### 错误恢复策略（不静默替换）

- `fail_fast`（默认）：首个错误即停止，解码器闭锁，需 `reset` 后才能复用；
- `skip`：报告错误后丢弃已消费的部分序列字节，把**当前字节重新当作新
  序列起点**继续解码（Unicode maximal-subpart 风格的重同步）。

两种策略都只通过结构化错误对象报告损坏，**绝不向输出插入 U+FFFD 或任何
替换字符**；损坏字节的范围由错误的 `sequence_start..offset` 精确给出。
（若输入本身合法地编码了 U+FFFD，它会作为普通码点原样通过。）

### 资源限制

`Limits { max_input_bytes, max_output_codepoints }` 限制自上次 `reset`
以来的累计输入字节数与累计输出码点数；默认 64 MiB / 16 Mi 码点，
`Limits::unlimited()` 可关闭。限制触发时无条件停止（不受恢复策略影响）。
解码器内部状态 O(1)，输出按 `feed` 调用分批返回，内存占用由调用方
块大小决定。

## JSON 控制协议（utf8ctl)

- 传输：stdin/stdout,**每行一个 JSON 对象**(NDJSON);UTF-8 编码；
- 每个请求产生且仅产生一行响应；进程内解码器跨请求保持状态，因此可以
  用多次 `decode` 请求模拟任意切块；
- 二进制数据一律用**小写 hex 字符串**(`data_hex`/`output_hex`）传输；
- 通用字段：`id`（请求原样回显，用于配对）、`ok`（布尔，无错误为
  `true`)。

### 请求 op 一览

| op         | 额外字段                                             | 说明                     |
|------------|------------------------------------------------------|--------------------------|
| `configure`| `limits:{max_input_bytes,max_output_codepoints}`、`recovery:"fail_fast"\|"skip"` | 仅允许在首次 decode 前或 reset 后 |
| `decode`   | `data_hex`                                           | 喂入一块字节流           |
| `finish`   | —                                                    | 标记流结束，检查截断序列 |
| `encode`   | `codepoints:[u32,...]`                               | 码点编码为 UTF-8 hex     |
| `reset`    | —                                                    | 重置解码器（保留配置）   |
| `stats`    | —                                                    | 累计消费/产出/挂起字节数 |

### 响应示例

```json
{"id":2,"ok":true,"codepoints":[20013,25991],"output_hex":"e4b8ade69687","errors":[],"stopped":false,"consumed":6,"emitted":2}
{"id":2,"ok":false,"errors":[{"kind":"truncated_sequence","offset":3,"sequence_start":3}],"consumed":5,"emitted":3}
{"id":1,"ok":false,"error":"unknown op \"nonsense\""}
```

错误对象字段：`kind`（上表 snake_case)、`offset`、`sequence_start`。
`stopped=true` 表示本次块未消费完（fail-fast 停止或触发限制）。解码器
闭锁后（fail-fast 停止、触发限制或已 `finish`)，后续 `decode` 请求返回
`stopped=true` 且不消费任何输入，需 `reset` 后才能继续。

### 请求样例

`examples/requests/` 下为可直接运行的样例（实际输出见下文「运行记录」):

- `01_decode_simple.jsonl` — 配置 + 单块解码 "中文";
- `02_decode_split_chunks.jsonl` — 🦀(`F0 9F A6 80`）按 2+2 字节跨块解码；
- `03_decode_invalid.jsonl` — 截断序列、过长编码、代理项、超范围；
- `04_recovery_skip.jsonl` — skip 恢复策略下损坏字节两侧的合法内容照常解码；
- `05_roundtrip.jsonl` — encode → decode 往返（含 U+10FFFF)。

## 测试与验收对应

| 验收要求                     | 测试文件                                   |
|------------------------------|--------------------------------------------|
| 所有切分点                   | `split_points.rs`（每个输入的全部 2 块切分点、短输入的全部 3 块切分对、逐字节喂入） |
| 非法字节组合                 | `invalid_sequences.rs`（过长/代理项/超范围/孤立续字节/非法前导，且每种都在所有切分点下验证） |
| 编码解码往返                 | `roundtrip.rs`（全部 1 114 112 个标量值，含逐字节喂入与多种块大小） |
| 错误恢复不静默替换           | `recovery.rs`（断言输出中绝不出现非输入自带的 U+FFFD，错误带精确偏移） |
| 内存/输出限制                | `limits.rs`                                |
| JSON 控制入口                | `cli.rs`（真实子进程端到端）               |

## 实际运行记录

环境：Linux 6.8.0-90-generic,x86_64；系统 Rust **1.75.0**
(`/usr/bin/cargo`、`/usr/bin/rustc`)。

> 环境备注：本机 `~/.cargo/bin` 下的 rustup 代理因网络问题（镜像校验和
> 错误、多进程争抢同一 toolchain 目录）无法完成 stable 工具链安装，故
> 改用系统自带的 Rust 1.75.0，全程 `--offline`（项目零依赖，不需要
> crates.io)。

```bash
# 构建
$ RUSTC=/usr/bin/rustc /usr/bin/cargo build --offline
   Compiling inc-utf8 v0.1.0
    Finished dev [unoptimized + debuginfo] target(s) in 1.16s

# 测试
$ RUSTC=/usr/bin/rustc RUSTDOC=/usr/bin/rustdoc /usr/bin/cargo test --offline
test result: ok. 10 passed; 0 failed   (tests/cli.rs)
test result: ok. 5 passed; 0 failed    (tests/invalid_sequences.rs)
test result: ok. 6 passed; 0 failed    (tests/limits.rs)
test result: ok. 7 passed; 0 failed    (tests/recovery.rs)
test result: ok. 5 passed; 0 failed    (tests/roundtrip.rs)
test result: ok. 5 passed; 0 failed    (tests/split_points.rs)
# 合计 38 个测试全部通过，0 失败

# clippy(无警告)
$ env -i PATH=/usr/bin:/bin RUSTC=/usr/bin/rustc /usr/bin/cargo clippy --offline --all-targets
    Checking inc-utf8 v0.1.0
    Finished dev target(s) — 0 warnings

# rustfmt
$ /usr/bin/rustfmt --edition 2021 --check src/*.rs src/bin/*.rs tests/*.rs
# 初次有 2 处格式差异，已用 rustfmt 修复并复检通过
```

请求样例实际输出（`./target/debug/utf8ctl < examples/requests/03_decode_invalid.jsonl`):

```json
{"id":1,"ok":true,"codepoints":[97,98,99],"output_hex":"616263","errors":[],"stopped":false,"consumed":5,"emitted":3}
{"id":2,"ok":false,"errors":[{"kind":"truncated_sequence","offset":3,"sequence_start":3}],"consumed":5,"emitted":3}
{"id":3,"ok":true}
{"id":4,"ok":false,"codepoints":[],"output_hex":"","errors":[{"kind":"overlong_encoding","offset":0,"sequence_start":0}],"stopped":true,"consumed":0,"emitted":0}
{"id":5,"ok":true}
{"id":6,"ok":false,"codepoints":[],"output_hex":"","errors":[{"kind":"surrogate","offset":1,"sequence_start":0}],"stopped":true,"consumed":1,"emitted":0}
{"id":7,"ok":true}
{"id":8,"ok":false,"codepoints":[],"output_hex":"","errors":[{"kind":"out_of_range","offset":1,"sequence_start":0}],"stopped":true,"consumed":1,"emitted":0}
```

### 开发过程中出现并已修正的问题（如实记录）

1. `tests/cli.rs::encode_roundtrip_via_cli` 首次运行**失败**：测试中断言
   的期望 hex 写错（把码点 128029 = U+1F41D 误当成 U+1F69D)。被测编码器
   输出 `f09f909d` 是正确的，修正测试期望值后通过。实现代码未改动。
2. `cargo test` 默认会连带构建 doctest，因 rustup 代理损坏报
   `can't find crate for std`；通过显式指定 `RUSTDOC=/usr/bin/rustdoc`
   解决，与项目代码无关。
3. rustfmt 初次检查有 2 处格式差异（`src/decoder.rs`)，已修复。

### 当前未通过项 / 已知限制

- 无未通过的测试。
- 已知限制：JSON 数字按 `f64` 处理，可精确表示的整数上限为 2^53（远超
  本项目偏移/上限的取值范围）;`utf8ctl` 为单解码器实例，如需并发会话
  请运行多个进程。
