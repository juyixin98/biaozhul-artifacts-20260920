# psst — 前缀压缩有序表（Prefix-Compressed Sorted Table）

一个纯后端、文件支持（file-backed）的**只读有序表**存储库，带本地 HTTP 验证入口。
设计参照 LevelDB SSTable 的子集：块内前缀压缩、重启点（restart points）、
块级 CRC32C 校验、稀疏两级索引；外加**严格的整文件格式校验**和**可注入 I/O 层**，
用于确定性地模拟磁盘故障。

无第三方依赖，标准库实现（CRC32C 为软件查表实现，HTTP/JSON 为手写最小实现）。

---

## 1. 构建与测试

```bash
cargo build --release
cargo test                 # 全部 54 个测试（含 40 个随机种子的差分测试）
cargo clippy --all-targets # lint（如已安装 clippy）
```

启动本地验证服务：

```bash
cargo run --release -- serve --dir ./tabledata --addr 127.0.0.1:8088
```

请求样例见 [`examples/http/requests.md`](examples/http/requests.md)，
一键演示脚本见 [`scripts/demo.sh`](scripts/demo.sh)。

---

## 2. 磁盘格式（明确的字节级约定）

所有多字节整数均为**小端**。长度前缀使用 varint（每字节低 7 位为载荷，最高位为续位）。

### 2.1 文件整体布局

```text
+-------------------+
| data block 0      |  块载荷 + 5 字节 trailer
+-------------------+
| data block 1      |
+-------------------+
| ...               |
+-------------------+
| metaindex block   |  本版本固定为空块（始终存在）
+-------------------+
| index block       |  每个 data block 一条稀疏索引项
+-------------------+
| footer            |  固定 48 字节
+-------------------+
```

### 2.2 块 trailer（紧跟每个块载荷，固定 5 字节）

```text
+---------------+--------------------+
| type : u8     | checksum : u32 LE  |
+---------------+--------------------+
```

* `type = 1`：原始（未压缩）数据块。本版本只允许该类型。
* `checksum`：**掩码后的 CRC32C（Castagnoli 多项式）**，计算输入为
  `type || 块载荷`。CRC 初值 0、末尾不异或（LevelDB 约定）；
  掩码为 `rotl15(crc) + 0xa282ead8`，避免载荷中恰好出现未掩码 CRC 时产生歧义。

### 2.3 块载荷（data 与 index 块同构）

```text
+--------------------------------------------------------+
| entry 0 | entry 1 | ... | restart[0..k-1] : u32 LE × k | num_restarts : u32 LE |
+--------------------------------------------------------+
```

每个 entry：

```text
shared_len   : varint32   # 与上一个键共享的前缀字节数
unshared_len : varint32   # 本键新增的后缀字节数
value_len    : varint32   # 值长度
key_delta    : unshared_len 字节
value        : value_len 字节
```

重建规则：`key_i = key_{i-1}[..shared_len] || key_delta`。

**重启点**：每隔 `restart_interval` 个条目（第 0 个必然是），`shared_len = 0`，
该条目存全键，其载荷偏移记入 restart 数组。重启点让查找无需从块首顺序解压：
先在重启点全键上二分，再在区间内线性前进。
index 块使用 `restart_interval = 1`（每条目都是全键）。

### 2.4 稀疏索引（两级）

index 块每个 data block 一条：

* key：**分隔键**（separator）。非末块为「本块最后一键 ≤ separator < 下一块第一键」
  的最短键（`FindShortestSeparator`，可能截断缩短）；末块为严格大于最后一键的
  `FindShortSuccessor`。
* value：该 data block 的 [`BlockHandle`](#25-block-handle)。

点查流程：在 index 块二分定位块句柄 → 只读那一个 data 块 → 块内重启点二分 + 线性定位。
与全量键索引相比，索引规模近似为「每块一项」，这是「稀疏」的含义。

### 2.5 Block handle

```text
offset : varint64
size   : varint64   # 块载荷长度，不含 5 字节 trailer
```

### 2.6 Footer（固定 48 字节）

```text
+----------------------------------------------------+
| metaindex_handle : varint64 + varint64             |
| index_handle     : varint64 + varint64             |
| zero padding （填 0 至 40 字节）                    |
| magic : u64 LE = 0x2025c1c3b050878e                |
+----------------------------------------------------+
```

### 2.7 空表

零条目表合法：没有 data block，index/metaindex 为空块（恰好是 4 字节的
`num_restarts = 0`），footer 正常。点查返回未命中，扫描为空，校验通过。

---

## 3. 同步边界（durability / synchronization boundary）

库对「什么时候字节才算落盘、文件什么时候才算存在」有明确约定：

1. **块是随满随刷的，但在 `TableBuilder::finish()` 返回前文件不是一个提交的表。**
   构建中途失败，磁盘上可能有部分块字节，但没有合法 footer，任何读取都失败。
2. **整个构建只有一次强制落盘**：`finish()` 写完 metaindex、index、footer 后
   调用**一次** `File::sync_all()`（数据 + 元数据）。此前不做逐块 fsync。
3. **文件级原子提交**（`build_table_sorted`）：先写临时名
   `.{name}.tmp.{unique}` → 一次 fsync → `rename(2)` 到最终名 →
   **对父目录再做一次 fsync**，保证目录项（重命名本身）落盘。
   失败路径删除临时文件；最终名已存在时显式报 `AlreadyExists`
   （Linux 的 rename 会静默覆盖，因此必须先检查），绝不覆盖一个可能正被打开的表。
4. 打开是**只读**的：`open()` 仅急读 footer 与 index 块（快速失败），
   data 块按需读取；打开后不持有任何写句柄。

---

## 4. 可注入 I/O 层（故障模拟）

所有读写都经过两个小 trait（`src/io.rs`），后端可替换：

* `WritableFile { append(&mut self, &[u8]) -> Result<usize>; sync(&mut self) }`
  —— `append` 允许**短写**（返回实际写入字节数），调用方必须循环补齐。
* `RandomAccessFile { read_at(&self, &mut [u8], offset) -> Result<usize> }`
  —— 允许**短读**，调用方循环直到 EOF。

内置实现：

| 实现 | 用途 |
|---|---|
| `PosixWriter` / `PosixReader` | 真实文件 |
| `MemWriter` / `MemReader`（共享 `MemStore`） | 确定性内存表，测试主后端 |
| `FaultyWriter` | 按调用序号注入 append 失败、部分写、sync 失败 |
| `FaultyReader` | 按调用序号注入 read 失败、碎片化读（每次最多 N 字节）、指定偏移短读 |

测试覆盖：注入写失败后构建中止且产物不可读；sync 失败从 `finish()` 上抛；
持续 3 字节短写时经重试产出与正常路径**字节完全一致**的文件；
7 字节碎片读下点查/扫描/校验结果全部正确（见 `tests/io_faults.rs`）。

---

## 5. 严格格式校验

`Table::validate()` 通读整个文件，强制：

* footer 恰好位于文件末尾且 magic 正确；句柄有界；
* data blocks 从偏移 0 起**紧密连续**排列，其后紧跟 metaindex、index、footer；
* 每个块：载荷/trailer 边界、block type、**CRC32C 掩码校验和**；
* restart 数组：数量有界、`restart[0] == 0`、偏移严格递增、不越入 restart 数组；
* 每个 entry：三个 varint 合法（拒绝截断/超长/溢出编码）、键值范围不越入 restart 数组、
  `shared_len` 不超过已重建前缀；
* 键在块内严格递增；**跨块**也严格递增；
* 每个 index 分隔键满足 `本块末键 ≤ sep < 下一块首键`，末块 successor 严格大于最后一键；
* metaindex 在本版本必须为空；index 条目数 == data block 数。

损坏测试（`tests/corruption.rs`）覆盖：footer magic 翻转、文件截断、载荷位翻转
（CRC 拦截）、block type 篡改、**损坏的重启偏移**（首项非 0 / 指向 restart 数组 /
非首项非递增——篡改后重算 CRC 以强制走到结构校验层）、entry 长度膨胀、
footer 句柄篡改、index 块位翻转、手工构造的跨块乱序。

---

## 6. HTTP 验证入口

仅用于本地验证，默认绑 `127.0.0.1`。键/值一律用**小写 hex 字符串**承载二进制。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/healthz` | 健康检查 |
| GET | `/tables` | 列出数据表 |
| PUT/POST | `/tables/{name}` | 由 JSON 条目构建表（自动排序、拒绝重复键） |
| DELETE | `/tables/{name}` | 删除表 |
| GET | `/tables/{name}/get?key=<hex>` | 点查 |
| GET | `/tables/{name}/scan?start=<hex>&end=<hex>&end_inclusive&start_exclusive&limit=N` | 范围扫描 |
| POST/GET | `/tables/{name}/validate` | 严格校验 |

构建请求体：

```json
{
  "block_size": 4096,
  "restart_interval": 16,
  "entries": [["<key hex>", "<value hex>"], ["...", "..."]]
}
```

错误统一为 `{"ok":false,"error_kind":"io|corruption|invalid_argument","error":"..."}`，
格式/校验错误返回 400，表不存在返回 404，重名返回 409。

---

## 7. 库 API 速览

```rust
use psst::io::{MemStore, MemWriter, MemReader};
use psst::table::{Options, Table, TableBuilder, ScanOptions, Bound, collect_scan};

// 写（内存后端示例；真实文件用 PosixWriter 或 build_table_sorted）
let store = MemStore::new();
let mut b = TableBuilder::new(MemWriter::new(store.clone()), Options::default())?;
b.add(b"apple", b"1")?;          // 必须严格递增
b.add(b"banana", b"2")?;
let stats = b.finish()?;         // 唯一的 sync 边界

// 读
let t = Table::open(MemReader::new(store.clone()), stats.bytes)?;
assert_eq!(t.get(b"apple")?, Some(b"1".to_vec()));
t.validate()?;                   // 严格整文件校验
let rows = collect_scan(&t, ScanOptions {
    start: Bound::Included(b"a".to_vec()),
    end:   Bound::Excluded(b"c".to_vec()),
    limit: Some(100),
})?;
```

真实文件用 `psst::table::build_table_sorted(dir, name, &entries, opts, nonce)?`
（自动排序、临时文件 + 原子 rename + 目录 fsync），再用 `Table::open_path(path)?`。

---

## 8. 模块与测试索引

```text
src/
  error.rs    错误类型（Io / Corruption / InvalidArgument）
  coding.rs   varint、小端定长、CRC32C + 掩码、hex
  format.rs   磁盘常量、BlockHandle、Footer、写 trailer
  io.rs       I/O trait + POSIX / 内存 / 故障注入实现
  block.rs    块构建/解析/游标/重启点二分/条目级校验
  table.rs    TableBuilder、Table（点查/扫描/validate）、原子文件构建
  server.rs   零依赖 HTTP 服务 + 最小 JSON
tests/
  random_diff.rs   40 个随机种子 × BTreeMap 差分（点查/全扫/区间扫/limit）
  edge_cases.rs    空键、长公共前缀、跨块扫描、空表
  corruption.rs    各类损坏（含损坏重启偏移，重算 CRC 直达结构层）
  io_faults.rs     故障注入（读写失败、短写短读、sync 失败）
  file_build.rs    真实文件的原子构建/重名/临时文件清理/区间扫
  http_server.rs   真实 TCP 端到端（构建/查/扫/校验/删除/坏请求/损坏上报）
```

## 9. 已知边界（scope）

* 只支持未压缩块（`type=1`）；无 Bloom 过滤器、无 WAL/MemTable、无 compaction、
  无多表合并——这是一个**只读有序表文件**及其构建器，不是完整 LSM 存储引擎。
* HTTP 服务为单进程本地验证用途，按线程处理连接，非高性能服务器，无鉴权。
