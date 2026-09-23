# sst — 前缀压缩有序表（只读 SSTable 风格存储）

纯后端 Rust 项目，零外部依赖。提供：

- 文件支持的只读有序表存储库（`src/` 库 crate）
- 可注入 I/O 层（`ReadAt` / `WriteSink` trait），测试可模拟写失败、sync 失败、撕裂写、读失败
- 本地 HTTP 验证入口与 CLI（`sst` 二进制）
- 严格格式校验（`verify`）

## 构建与测试

```bash
cargo build --release
cargo test
```

## 磁盘格式

全部整数为小端。文件布局：

```
+----------------+  <-- 偏移 0
| data block 0   |
| data block 1   |
| ...            |
| index block    |  <-- footer.index_offset
| footer (32B)   |  <-- file_len - 32
+----------------+
```

### 数据块 / 索引块（同一格式）

```
| entry 0 | entry 1 | ... | entry N-1            条目区
| restart[0] u32 ... restart[R-1] u32            重启点偏移数组（相对块起始）
| num_restarts u32
| crc32 u32                                      覆盖此前全部字节（IEEE 多项式）
```

单个 entry：

```
shared_len   varint   与前一条键的公共前缀长度（重启点处必须为 0）
unshared_len varint   键剩余部分长度
value_len    varint
key_delta    [u8; unshared_len]
value        [u8; value_len]
```

每 `restart_interval` 个条目设一个重启点（shared_len = 0，键完整存储），
点查时先对重启点二分、再在重启区间内线性解码。

### 索引块（稀疏索引）

索引块本身也是上述块格式（restart_interval = 1）。每个条目：

- 键：对应数据块的**最后一个键**（分隔键）
- 值：`varint(block_offset) ++ varint(block_size)`（block_size 含 CRC）

点查时在索引中找第一个 `分隔键 >= 目标键` 的块，只读这一块。

### 页脚（定长 32 字节，位于文件末尾）

```
| index_offset u64 | index_size u64 | num_entries u64 | magic u64 |
```

magic = `0x5353_5442_4C45_3031`（"SSTBLE01"）。

## 同步边界

- `TableWriter::finish` 依次写入：最后一个数据块 → 索引块 → 页脚，最后调用
  `WriteSink::sync`（文件实现为 `fsync`/`sync_data`）。**只有 sync 成功返回，
  表才被视为完整持久。**
- `write_all` 只保证字节进入内核缓冲；进程崩溃时未 sync 的字节可能丢失，
  此时文件尾部不完整，打开时会因魔数/索引边界校验失败而被拒绝（见
  `tests/fault_injection.rs` 的撕裂写用例）。
- 表构建完成后只读；不支持原地修改。

## 严格校验（verify）

`TableReader::verify` / `sst verify` / `GET /verify` 做全量检查：

- 页脚魔数、索引边界、索引块恰好结束于页脚起始
- 每个块：CRC32、条目可完整解码、shared_len 不超过前一键长度、键严格递增、
  条目区无空洞
- 重启数组：首项为 0、严格递增、每项落在条目边界、重启点处 shared_len 为 0
- 数据块从偏移 0 起连续排布、跨块键序递增、索引分隔键等于块内最后键
- 条目总数与页脚一致

## 可注入 I/O 层

```rust
pub trait ReadAt: Send + Sync { fn read_at(&self, buf: &mut [u8], offset: u64) -> Result<()>; fn len(&self) -> Result<u64>; }
pub trait WriteSink: Send { fn write_all(&mut self, buf: &[u8]) -> Result<()>; fn sync(&mut self) -> Result<()>; }
```

实现：`FileReader` / `FileWriter`（文件）、`MemStorage`（内存，测试/演示）、
`FaultyWriter`（故障注入：指定第 N 次写失败、撕裂写只落前 k 字节、sync 失败）。

## CLI

```bash
# 生成 5000 条随机 kv（hexkey=hexvalue 行），构建表（键自动排序，重复键报错）
./target/release/sst gen --n 5000 --seed 7 --prefix 757365723a \
  | ./target/release/sst build --out /tmp/demo.sst --block-size 512 --restart-interval 8

./target/release/sst verify --table /tmp/demo.sst
./target/release/sst get    --table /tmp/demo.sst --key 757365723a00
./target/release/sst scan   --table /tmp/demo.sst --start 7573 --end 7574 --limit 10
./target/release/sst serve  --table /tmp/demo.sst --addr 127.0.0.1:18080
```

键值在 CLI/HTTP 接口中一律用十六进制表示（空键 = 空串）。

## HTTP 验证入口

`sst serve --table FILE --addr 127.0.0.1:18080`，仅 GET，JSON 响应：

| 路径 | 说明 |
|---|---|
| `GET /health` | 存活探针 |
| `GET /stats` | 条目数、块数、文件大小 |
| `GET /get?key=<hex>` | 点查 |
| `GET /scan?start=<hex>&end=<hex>&limit=N` | 范围扫描 `[start, end)`，end 可省略 |
| `GET /verify` | 严格校验，失败返回 HTTP 500 + 错误详情 |

请求样例见 [examples/requests.sh](examples/requests.sh)。

## 自动化测试（36 个，全部通过）

- `src/` 单元测试 ×13：varint 边界、CRC32 标准向量、hex、块往返/seek/空块/损坏重启点/非法 shared_len
- `tests/random_vs_model.rs` ×7：随机键对照 `BTreeMap`（点查命中/未命中、全表扫描、200 组随机范围扫描），
  覆盖空键空值、300 字节长公共前缀、跨块扫描（128B 小块）、restart_interval=1、超大值跨块、空表、乱序/重复键拒绝
- `tests/corruption.rs` ×10：损坏重启偏移（越界/指向条目中间）、CRC 不匹配、非法 shared_len、
  截断、坏魔数、索引越界、条目数不符、索引分隔键不符 —— 全部要求报错而非 panic/错数据
- `tests/fault_injection.rs` ×5：sync 失败、逐写调用点失败注入、撕裂写（页脚/数据块）、读失败
- `tests/http_smoke.rs` ×1：真实启动 `sst serve`，HTTP 请求验证全部端点与错误码

## 实际运行记录（2026-09-23，rustc 1.90.0，Linux x86_64）

```
$ cargo test
test result: ok. 13 passed; 0 failed   (src 单元测试)
test result: ok. 10 passed; 0 failed   (tests/corruption.rs)
test result: ok.  5 passed; 0 failed   (tests/fault_injection.rs)
test result: ok.  1 passed; 0 failed   (tests/http_smoke.rs)
test result: ok.  7 passed; 0 failed   (tests/random_vs_model.rs)
合计 36 passed; 0 failed

$ ./target/release/sst gen --n 5000 --seed 7 --prefix 757365723a | ./target/release/sst build --out /tmp/demo.sst --block-size 512 --restart-interval 8
{"entries":5000,"blocks":267,"file_size":146706}

$ ./target/release/sst verify --table /tmp/demo.sst
{"ok":true,"blocks":267,"entries":5000,"first_key":"757365723a00086fb5397148f0","last_key":"757365723afffccd2a369f3dde","file_size":146706}

$ ./target/release/sst serve --table /tmp/demo.sst --addr 127.0.0.1:18080 &
$ curl -s http://127.0.0.1:18080/stats
{"entries":5000,"blocks":267,"file_size":146706}
$ curl -s "http://127.0.0.1:18080/get?key=757365723a00086fb5397148f0"
{"found":true,"key":"757365723a00086fb5397148f0","value":"920326b63bf02a25"}
$ curl -s "http://127.0.0.1:18080/get?key=ff"
{"found":false}
$ curl -s "http://127.0.0.1:18080/scan?start=757365723a&limit=2"
{"entries":[["757365723a00086fb5397148f0","920326b63bf02a25"],["757365723a000ee30e14653d77","a393852888231f45"]],"count":2,"truncated":true}
$ curl -s http://127.0.0.1:18080/verify
{"ok":true,"blocks":267,"entries":5000,...}
$ curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:18080/get?key=zz"   # 坏 hex
400
$ curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:18080/nope           # 未知路径
404

# 损坏演示：翻转第一个数据块 1 字节后
$ curl -s http://127.0.0.1:18081/verify        # HTTP 500
{"ok":false,"error":"corruption: block crc mismatch: stored 0xec0568bb, computed 0x14098dae"}
$ ./target/release/sst verify --table /tmp/demo-corrupt.sst; echo exit=$?
error: corruption: block crc mismatch: stored 0xec0568bb, computed 0x14098dae
exit=1
```

未通过项：无。

## 已知限制

- 表构建后只读；更新需重建。
- 范围扫描为惰性迭代但无块缓存，点查每次读一个块（含 CRC 校验）。
- 页脚本身无 CRC，依赖魔数与交叉一致性检查（条目数、索引边界）发现损坏。
- 文件读取使用 Unix `read_exact_at`（仅 Linux/Unix）。
