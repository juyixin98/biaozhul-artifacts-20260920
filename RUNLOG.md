# 运行记录（Run Log）

日期：2026-09-25
平台：Linux x86_64
Rust：rustc/cargo 1.98.1（stable，2026-09-01/2026-08-05），零第三方依赖

> 环境备注：本机 rustup 的 stable 工具链安装曾损坏（组件重命名竞态），
> 通过官方 tarball 将 rustc/cargo/rust-std 1.98.1 安装到 `~/rust-local` 后构建，
> 构建命令前置 `PATH=$HOME/rust-local/bin:$PATH`。项目本身不需要此变通。

## 1. 构建

```text
$ cargo build --release
   Compiling bschema v0.1.0
    Finished `release` profile [optimized] target(s) in 1.86s
```

产物：`target/release/bschema`（约 740 KB，无动态库依赖问题）。

## 2. 自动化测试

```text
$ cargo test
running 15 tests  (src 单元测试: varint/json/base64/ieee/wire/schema)
test result: ok. 15 passed; 0 failed

running 25 tests  (tests/integration.rs)
test result: ok. 25 passed; 0 failed

$ cargo clippy --all-targets -- -D warnings
CLIPPY EXIT: 0
$ cargo fmt
```

集成测试覆盖（全部通过，无跳过）：

1. 全标量类型往返，含 2^53 以上整数、u64 最大值、i32 边界
2. 特殊 double（−0、无穷大、最小正规格化数、f64::MAX）位级往返
3. 显式零值（id=0、name=""）与缺失字段的区分
4. 必填字段在 JSON 转换、编码、解码三处均被强制
5. 新 schema 读旧数据
6. 旧 schema 读新数据，未知字段（含 unpacked 的逐值条目）保留
7. 未知字段经中间 schema **逐字节**转发（空 schema 视角比对载荷）
8. 嵌套消息内部的未知字段转发
9. packed/unpacked 互读，以及线上交错混读
10. string→int64 类型不兼容被拒绝（错误带字段号与类型名）
11. int32↔int64 加宽恒成功，收窄遇越界值失败
12. 非负 varint 类型拒绝负数（提示用 sint64）
13. 逐切点截断（对 134 字节消息的所有严格前缀）均为结构化错误
14. 声明长度超出流 → 截断错误
15. 非规范 bool(2) 拒绝
16. 未知 tag id 15 拒绝
17. 坏魔数 / 坏版本拒绝
18. 输入字节上限（含未知字段字节计入）
19. 输出字节上限
20. 嵌套深度上限（编/解码双侧）
21. 重复数量上限
22. 流式解码与缓冲解码结果一致
23–25. JSON 请求信封 encode/decode/forward 全链路及错误返回

## 3. 端到端验收脚本

```text
$ ./scripts/demo.sh
== 1. fingerprints (rename-safe, version-distinct) ==
v1=46e0f7f257792427 v2=190700af3723e33b
PASS: v1 != v2 fingerprint
PASS: same-schema fingerprint match
PASS: unknown nickname retained
PASS: forwarded body byte-identical
PASS: nickname recovered
PASS: nested unknown recovered
PASS: old data still decodes
PASS: string->int64 rejected
PASS: explicit id=0 present
PASS: missing email omitted
PASS: truncated input rejected
PASS: byte limit enforced
RESULT: 12 passed, 0 failed
```

完整输出另存于 `build/demo-output.txt`。

## 4. 请求信封样例（examples/requests/）

| 请求 | 退出码 | 说明 |
|------|--------|------|
| 01_encode_v2 | 0 | 134 字节落盘 build/person_v2.bse1 |
| 02_decode_with_v2 | 0 | fingerprint_match=true |
| 03_decode_new_data_with_old_schema | 0 | 根 4 个未知条目 + Address.country 嵌套未知 |
| 04_forward_through_v1 | 0 | 134→134 字节 |
| 05_decode_forwarded_with_v2 | 0 | nickname/tags/country/balance 全部恢复 |
| 06_decode_incompatible_type | **1** | `incompatible type for field 3 (email): expected wire type varint, found string` |
| 07_encode_explicit_zeros | 0 | 输出 18 字节 |
| 08_fingerprint_v1 | 0 | 46e0f7f257792427 |

完整响应见 `build/requests-output.txt`。

## 5. 关键观测：转发字节级无损

```text
$ tail -c +15 build/person_v2.bse1     > body_orig.bin   # 跳过 14 字节头
$ tail -c +15 build/person_through_v1.bse1 > body_fwd.bin
$ cmp body_orig.bin body_fwd.bin
（相同，退出 0）
```

v2 数据经"只认识 v1"的代理解码再编码后，**消息体逐字节相同**，仅文件头的
schema 指纹随代理的 schema 改变（这是预期：指纹标识写入者 schema 版本）。

## 6. 显式零值的线格式印证

`{"id":0,"name":""}` 的十六进制：

```text
4253 4531 0100 46e0 f7f2 5779 2427 1000 2700
BSE1   v1 f=0  fingerprint(v1)      f1v f2s L0
```

- `0x10 = (1<<4)|0`：字段 1，VARINT；`0x00`：显式值 0
- `0x27 = (2<<4)|7`：字段 2，STRING；`0x00`：长度 0（显式空串）
- 没有字段 3/4/5 的任何字节——缺失即线上无标签；解码 JSON 只有 `id`、`name` 两键。

## 7. 未通过项 / 已知限制

无未通过的验收项。已知设计边界（非缺陷）：

1. **根消息无长度前缀**：以干净 EOF 结束，因此严格前缀若恰好停在根消息的
   条目边界上，会被视为一条更短的合法消息（流式分帧）；所有嵌套消息有长度前缀，
   其截断一律报错。需要消息边界自描述的传输场景可自行在外层加帧。
2. 重复字段不区分"未设置"与"空数组"（二者线上均无条目）；单值字段严格区分。
3. `int32/int64` 仅承载非负整数；负数必须用 `sint32/sint64`（编码侧强制）。
4. JSON 中 NaN/Infinity 输出为 `null`（保证合法 JSON）；二进制侧完整保留位模式。
5. CLI 子命令读完整 stdin/文件到内存；库 API 提供真正的流式
   `encode_to_stream`/`decode_from_stream`。
