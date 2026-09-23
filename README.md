# resp-incremental — 手写增量 RESP2 解析库 + 本地 TCP 测试服务

纯 Rust、**零第三方依赖**（仅 `std`）实现的 RESP2（Redis Serialization Protocol
第 2 版）**增量字节解析器**，外加一个用于在真实 TCP 上验证它的极简内存
K/V 服务与离线解析工具。核心解析器完全手写，**没有使用任何现成的协议解析
crate**（`Cargo.toml` 无 `[dependencies]`，可自行核对）。

---

## 1. 明确支持的 RESP2 子集

| 报文形态 | 解析为 | 说明 |
|---|---|---|
| `+OK\r\n` | `Value::Simple(String)` | 简单字符串，载荷必须是 UTF-8 |
| `-ERR msg\r\n` | `Value::Error(String)` | 错误回复 |
| `:42\r\n` | `Value::Integer(i64)` | 64 位有符号整数（含 `i64::MIN/MAX`） |
| `$5\r\nhello\r\n` | `Value::Bulk(Vec<u8>)` | **二进制安全**，载荷中可以有任意字节，包括 `\r\n` |
| `$0\r\n\r\n` | `Value::Bulk(vec![])` | **空串**：存在，长度为 0 |
| `$-1\r\n` | `Value::Null` | **空值**（null bulk） |
| `*-1\r\n` | `Value::Null` | null 数组，RESP2 中与 null bulk 同为 nil |
| `*2\r\n…\r\n…` | `Value::Array(Vec<Value>)` | 数组，可任意嵌套 |

**明确不支持**：RESP3 类型（`_`、`,`、`#`、`=`、`%`、`~`、`|`、`>` 等）、
内联（inline）命令。遇到不认识的类型字节立即报 `UnknownTypeByte`。

### 空值 vs 空串

二者在线上是不同的字节、在模型里是不同的枚举变体，解析和回编都严格区分：

```text
空串  $0\r\n\r\n   -> Value::Bulk(vec![])   is_empty_bulk() == true,  is_null() == false
空值  $-1\r\n      -> Value::Null           is_null()       == true
```

TCP 服务也遵循 Redis 语义：`GET` 一个不存在的键返回 `$-1\r\n`（null），
而存在但值为空串的键返回 `$0\r\n\r\n`（empty bulk）。

---

## 2. 可配置限制（长度上限 / 深度 / 总字节预算）

`parser::Config`（`src/parser.rs`）：

| 字段 | 默认值 | 超限错误 |
|---|---|---|
| `max_bulk_length` | 512 MiB | `BulkTooLarge{declared,max}` |
| `max_array_length` | 1,000,000 | `ArrayTooLarge{declared,max}` |
| `max_depth`（最外层数组为 0） | 7 | `NestingTooDeep{depth,max}` |
| `max_line_length`（`+`/`-`/`:`/`$`/`*` 头行内容上限） | 64 KiB | `LineTooLong{length,max}` |
| `max_total_bytes`（**跨所有已完成帧的累计字节预算**） | 64 MiB | `BudgetExceeded{needed,remaining}` |

- 深度定义：最外层数组处于深度 0，每嵌套一层 +1。`max_depth = 2` 允许
  `[[[]]]`，拒绝 `[[[[]]]]`（进入深度 3 时报错）。
- 总预算**只对已经完整解析出来的帧计费**；还没拼完整的帧一个字节都不计。
  在数组内部，头行、所有子帧、CRLF 全部计入预算（逐字节透传，无重复计数）。
- 一旦某帧按其声明长度就不可能放进剩余预算，即使载荷还没到也立即失败。

### 错误类型一览（`src/error.rs` 的 `ParseError`）

`UnknownTypeByte`、`InvalidLineEnding{byte}`（含裸 `\n`、`\r` 后非 `\n`）、
`InvalidInteger{field,raw}`、`IntegerOverflow(raw)`、
`InvalidBulkLength(n)`（**只有 `-1` 合法，`-0`/`-2`/… 全部非法**）、
`BulkTooLarge`、`ArrayTooLarge`、`NestingTooDeep`、`BudgetExceeded`、
`LineTooLong`、`InvalidUtf8`、`ParserPoisoned`。

任何致命错误都会让 `Parser` 进入 **poisoned** 状态：RESP 没有自再同步标记，
服务器应回复一个错误后关闭连接（TCP 服务就是这么做的）。

---

## 3. 增量解析模型

```rust
let mut p = Parser::new(Config::default());
p.feed(&stream_chunk)?;                 // 任意 TCP 分块，可反复调用
match p.try_next() {
    Poll::Ready(value) => { /* 一个完整帧，已从内部缓冲移除并计入预算 */ }
    Poll::Pending        => { /* 数据不够，继续 feed，不是错误 */ }
    Poll::Error(e)       => { /* 致命协议错误，解析器已 poisoned */ }
}
```

- `feed` 接受**任意切分**：一个字节、半行、半个 CRLF、bulk 载荷中间、
  多条报文粘在一起，都正确。
- bulk 按声明长度**数字节**，绝不扫描载荷里的 CRLF，因此
  `$6\r\nab\r\ncd\r\n` 里的 `\r\n` 被当作普通数据。
- 支持**多报文连续输入（pipeline）**：`try_next` 每成功一帧就从缓冲头部
  删除对应字节，剩余字节留给下一帧。

---

## 4. 目录结构

```text
Cargo.toml                  零依赖清单（无 [dependencies]）
src/
  lib.rs                    库入口 / 文档
  error.rs                  ParseError、Poll
  value.rs                  Value 模型 + RESP2 回编 encode
  parser.rs                 手写增量解析器 + Config（核心）
  bin/server.rs             本地 TCP 测试服务（PING/ECHO/SET/GET/DEL/QUIT…）
  bin/dump.rs               离线解析工具 resp-dump（按 64B 分块喂入）
tests/
  parser_unit.rs            30 个：各类型、空值/空串、二进制、限制、错误
  split_tests.rs            9 个：**全部切分点**穷举 + 随机多段切分
  roundtrip.rs              5 个：解析↔回编往返
  server_tcp.rs            15 个：真实起服务、原始 socket、逐字节/逐切点
samples/                    原始 RESP 请求样例（.resp，含 xxd 说明见下）
scripts/
  verify.sh                 一键：build + clippy(-D warnings) + test + TCP 冒烟
  resp_client.py            零依赖 Python 原始 socket 客户端（可逐字节慢送）
docs/run_log.txt            最近一次 verify.sh 的完整命令与输出记录
```

测试总数：**60**（30 + 9 + 5 + 15 + 1 doc-test），全部自动化。

---

## 5. 快速开始

```bash
cargo build --release

# 启动 TCP 服务（默认 127.0.0.1:6379；--port 0 为随机端口并打印 LISTENING）
./target/release/resp-server --addr 127.0.0.1 --port 6379

# 另一个终端
nc 127.0.0.1 6379 < samples/01_ping.resp
# -> +PONG

# 逐字节慢送（每字节 5ms），验证增量解析对任意分块透明
python3 scripts/resp_client.py samples/04_echo_embedded_crlf.resp \
    --port 6379 --per-byte 0.005

# 离线解析（按 64 字节分块喂入同一个解析器），清晰展示 空 bulk vs Null
./target/release/resp-dump samples/07_empty_and_null.resp
```

`resp-server` 的限制可通过命令行覆盖：

```bash
./target/release/resp-server --port 6379 \
    --max-bulk-bytes 4 --max-array-len 100 \
    --max-depth 2 --max-line-len 4096 --max-total-bytes 1048576
```

### 一键验证

```bash
bash scripts/verify.sh        # build(release+debug) + clippy -D warnings + cargo test + TCP 冒烟
# 完整命令与输出同时写入 docs/run_log.txt
```

---

## 6. 请求样例（`samples/`）

| 文件 | 内容 | 期望 |
|---|---|---|
| `01_ping.resp` | `*1 PING` | `+PONG\r\n` |
| `02_set.resp` | `SET fruit mango` | `+OK\r\n` |
| `03_get.resp` | `GET fruit` | `$5\r\nmango\r\n` |
| `04_echo_embedded_crlf.resp` | `ECHO "ab\r\ncd"`（载荷**内含 CRLF**） | `$6\r\nab\r\ncd\r\n` |
| `05_pipeline.resp` | PING + SET + GET 粘在一条流里 | 三条回复依次返回 |
| `06_nested.resp` | 嵌套数组 `*2[:100, *2[$2 hi, $-1]]` | 用 `resp-dump` 查看结构 |
| `07_empty_and_null.resp` | 先 `$0\r\n\r\n` 再 `$-1\r\n` | dump 显示 `Bulk(len=0)` 与 `Null` 两帧 |
| `08_bad_neg_length.resp` | `$-2\r\n`（非法负长度） | 错误后关连接；dump 退出码 1 |
| `09_bare_lf.resp` | `+OK\n`（裸 LF） | `InvalidLineEnding`，退出码 1 |

样例都是原始字节，建议用 `xxd samples/04_echo_embedded_crlf.resp` 查看真实
字节（能看到载荷内部的 `0d0a`）。

---

## 7. “全部切分点”测试是怎么做的

`tests/split_tests.rs` 对每个样例字节串 `w`：

1. **单点切分**：对每个 `k = 1..len`，先发 `w[..k]` 再发 `w[k..]`，每一步都
   尝试 `try_next`；
2. **逐字节**：一次只喂 1 个字节；
3. **随机多段**：用确定性 LCG 生成 64 组 2–4 段切分（可复现）。

无论怎么切，解析出的帧序列必须与“一次性整段喂入”**逐字节相等**；
致命错误样例则要求错误种类在任何切分下都不变。另在“紧限制配置”
（bulk≤16、depth≤2、总预算 512）下重复同样的不变量。

任务点名的三个重点都有专门用例：
- **CRLF 出现在 bulk 内部**：`crlf_inside_bulk_is_not_mistaken_for_terminator_at_every_cut`
  等，payload `"a\r\nb\r\nc"` 含两个内部 CRLF；
- **负长度非法值**：`negative_bulk_length_variants`（`-1` 合法，`-0/-2/-1000000` 致命）；
- **深嵌套 + 多报文连续输入**：`deep_nesting_plus_following_messages`，
  一个贴着深度上限的帧后面紧跟 `+OK`、`:2`，任何切分都不得丢失尾部两帧。

---

## 8. TCP 测试服务支持的命令

`PING [msg]`、`ECHO msg`、`SET k v`、`GET k`、`DEL k…`、`COMMAND`、`QUIT`。
这只是一个用来压解析器的内存 K/V，不是 Redis；命令必须是 RESP 数组、
键/值必须是 bulk（含空 bulk）。协议层错误（坏字节、超深度、超预算等）
返回 `-ERR protocol error: …` 后**关闭连接**。

---

## 9. 实际运行记录（节选）

完整记录见 [`docs/run_log.txt`](docs/run_log.txt)（`scripts/verify.sh` 生成）。

- `cargo build` / `cargo build --release`：成功，无警告；
- `cargo clippy --all-targets -- -D warnings`：干净；
- `cargo test`：**60/60 通过，0 失败**；
- TCP 冒烟（真实 release 服务 + `nc`）：ping / 内含 CRLF 的 echo /
  GET 缺失键返回 `$-1` / 空串键返回 `$0…` 四项全过；
- CLI 限制实测：bulk 长度 5 > 上限 4 报 `BulkTooLarge`；嵌套到深度 2 >
  `--max-depth 1` 报 `NestingTooDeep`；总预算 40 时前两个 PING 成功、
  第三个报 `BudgetExceeded { needed: 6, remaining: 4 }`；
- 非法样例 `$-2` 与裸 `\n` 经 `resp-dump` 解析均以退出码 1 报出明确错误；
  不完整输入（`:42\r\n:7\r`）正确得到第一帧后报告 3 字节悬挂、退出码 1。

开发期间观察到的**唯一一次测试失败**：在手工还跑着两个占用端口的服务时
执行完整并行 `cargo test`，1 个 TCP 用例受外部时序干扰失败；停掉手工服务、
并将该用例单独连跑 3 次后稳定通过。该现象不是解析器缺陷（服务端日志无
panic），已如实记录于此。

## 10. 工具链

在 `cargo 1.98.1` / `rustc 1.98.1`（Linux x86_64）下开发验证，仅用稳定版
特性与标准库。
