# 实测记录（RUNLOG）

日期：2026-09-25
环境：Ubuntu (Linux 6.8.0-90-generic, x86_64)
工具链：最终使用 apt 安装的 **rustc/cargo 1.75.0**（项目零依赖，仅用 stable
通用特性，1.75 可编译；开发期间一度尝试 rustup 安装 1.98.1，因沙箱内多个
超时残留进程并发写同一 `~/.rustup` 目录反复失败，详见文末"环境备注"）。

## 1. 构建

```bash
$ cargo build --release
    Finished release [optimized] target(s)
$ ls -la target/release/chfc
-rwxrwx-rx ... target/release/chfc  (约 7.3 MB)
```

无外部依赖（`Cargo.toml` 中 `[dependencies]` 为空）。

## 2. 自动化测试

```bash
$ cargo test
test result: ok. 9 passed    # src 内单元测试（huffman/format/bits）
test result: ok. 1 passed    # tests/fuzz_corrupt.rs：位翻转模糊（500 次损坏）
test result: ok. 7 passed    # tests/json_api.rs：JSON 控制入口
test result: ok. 20 passed   # tests/roundtrip.rs：往返/截断/伪造长度/等频率/限制
test result: ok. 1 ignored   # tests/stress.rs：2000 轮随机压力（默认忽略）

$ cargo test --test stress -- --ignored
test result: ok. 1 passed (8.53s)   # 2000 轮随机长度/字母表/块大小往返
```

合计 **37 个用例全部通过**（不含忽略项为 37 个二进制级用例；另有 2000 轮压力）。

关键覆盖：

- 空输入、单符号（0/7/255 三种字节，含单字节输入）、两符号
- 200 轮随机字节往返（确定性 xorshift，随机长度 0..4096、随机字母表大小、
  随机块大小 1..512，强制大量多块与块边界情形）
- **截断位流**：砍掉载荷末尾 1~2 字节 → `truncated_stream` /
  `unexpected_end_of_code`
- **伪造长度**：改写 `original_len` → 长度不一致/表校验错误；
  改写 `payload_bits` 为超大值 → 表校验错误或截断
- 手工构造 **过度订阅** 码长表（3 个长度 1：Kraft=1.5）→ `oversubscribed`
- 手工构造 **不完整** 码长表（长度 1,2：Kraft=0.75）
  → 默认 `reject` 报 `incomplete_code`；`allow` 下合法位串可解、
  走到未定义位串 `11` 报 `undefined_codeword`
- **等频率**：4/8 个等频符号码长一致且码本确定；100 轮全 256 字节等频数据往返
- 非零填充位、BFINAL 后尾随字节、魔数损坏、版本检查
- `max_block_bytes` / `max_output_bytes` 两个上限均触发
- 长度恰为块大小整数倍（1~4 块 × 1/3/16/100 字节）的 EOF 前瞻边界
- JSON：base64/hex 往返、错误信封、缺字段、非法 base64、限制透传、ratio 字段

## 3. 静态检查

```bash
$ rustfmt --edition 2021 --check <全部源文件>
（无输出 = 通过）
$ cargo clippy --all-targets -- -D warnings
（无 error / warning，退出码 0）
```

源文件最大 623 行（jsonapi.rs），均在 800 行软上限内。

## 4. 端到端 CLI 实测（release）

对 7 类输入压缩→解压→`cmp` 字节比对，**全部 ROUNDTRIP_OK**：

| 输入 | 原始字节 | 压缩字节 | ratio | 说明 |
|---|---:|---:|---:|---|
| empty.bin | 0 | 25 | 0 | 空输入固定 25 字节（前奏6+块头19） |
| single.bin | 100000 | 12527 | 0.1253 | 单一符号（每字节约 1 位+头/表） |
| text.txt | 135000 | 75456 | 0.5589 | 英文句子重复 |
| structured.bin | 100000 | 20834 | 0.2083 | 3 符号偏斜分布 |
| random.bin | 100000 | 100537 | 1.0054 | 密码学随机字节，**变大** |
| equal.bin | 100000 | 100537 | 1.0054 | 256 符号等频率，**变大** |
| small.bin | 16 | 65 | 4.0625 | 极小高熵输入，**明显变大** |

如 README 所述，**不保证输出变小**：随机/等频率数据压缩率约 1.005（仅头部
开销），16 字节小输入因固定头/表开销膨胀到 4 倍。这些均如实输出。

### 流式 / 多块 / 管道

```text
text.txt 以 --block-size 1000 压缩：135 块，ratio 0.6332，往返一致 MULTI_BLOCK_OK
cat text.txt | chfc compress -i /dev/stdin -o /dev/stdout | \
  chfc decompress -i /dev/stdin -o /dev/stdout | cmp - text.txt  => PIPE_OK
```

### 10 MiB 内存占用（块大小 64 KiB，160 块）

```text
compressed 10485760 -> 6289242 bytes in 160 block(s), ratio 0.5998
压缩最大驻留内存：2176 KB
解压最大驻留内存：2304 KB
往返：BIG_ROUNDTRIP_OK
```

内存与块大小而非输入总长相关，符合流式设计目标。

### 错误路径

```text
截断文件（末尾删 10 字节） -> error: truncated bit stream            (exit 1)
魔数损坏                    -> error: bad magic bytes                 (exit 1)
--max-output 50000          -> declared output length 100000 exceeds limit 50000
--max-block 50000           -> limit exceeded for block original_len
--incomplete-policy allow   -> 正常文件照常解压
```

### JSON 控制入口

```bash
$ echo '{"op":"compress","data":"aGVsbG8="}' | ./target/release/chfc json
{"ok":true,"op":"compress","result":{...}}
$ ./target/release/chfc json examples/decompress_request.json
ok: True
decoded: b'Hello, canonical Huffman! Hello, canonical Huffman! Hello!'
```

## 5. 开发过程中发现并修复的真实缺陷（如实记录）

1. **Base64 尾量子解码错误**：初版把合法填充尾部（2 个数据字符+"="
   时残留的 4 位、3 个数据字符+"=" 时残留的 2 位）一律当作
   "dangling bits" 拒绝，导致所有长度 mod 3 不为 0 的输入无法解码。
   由测试 `json_compress_reports_ratio` 暴露，已重写尾量子校验
   （按 `n_data % 4` 区分 0/2/3 三种合法余数并检查填充位为 0）。
2. **Kraft 边界判定**：规范码生成初版对"最后一个码之后恰好溢出"
   的过度订阅判定不完整，改为显式跟踪 `at_power_of_two`（码值达到
   2^length）状态，分别正确报 oversubscribed / incomplete。
3. 若干编译期问题：assert 分隔符笔误、Display 缺少新增变体分支、
   u8 与 255 的恒真比较（clippy `absurd_extreme_comparisons`）等，
   均已修复并纳入 clippy `-D warnings`。

## 6. 已知限制（不做超出要求的承诺）

- Huffman 仅利用单字节符号频率，不做 LZ77/上下文建模；高熵数据不压缩。
- 无完整性校验和：载荷位被翻转为另一合法码字时会解出不同内容。容器层
  提供的是**结构**校验（长度自洽、Kraft、截断、填充、尾随），不是密码学
  完整性；如需防篡改应在外层加校验和/签名。
- 单符号块用全 0 码流 + `payload_bits == original_len` 表达，每符号 1 位，
  这是对单符号情形的明确约定（见 README 格式规范）。

## 7. 环境备注（工具链安装过程，如实记录）

- 沙箱初始无 Rust。先尝试官方 rustup 脚本安装 stable（1.98.1），多次因
  工具命令超时被转入后台、残留的多个 `rustup`/`rustup-init` 进程并发写
  同一 `~/.rustup` 目录而互相破坏（"could not rename ... No such file"、
  "detected conflict"），清理全部残留进程后单独安装仍因官方 CDN 到本机
  仅约 40 KB/s（363 MB 需数小时）放弃。
- 最终改用本机 apt（阿里云镜像）安装 `cargo`/`rustc`/`rustfmt`/
  `rust-clippy` 1.75.0，秒级完成，构建与全部测试正常。
- 该过程未修改任何项目源码；项目本身对工具链无特殊要求。
