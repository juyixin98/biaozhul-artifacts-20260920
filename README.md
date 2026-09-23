# seglog — 分段追加日志 · 崩溃恢复服务 (Rust + Axum)

纯后端 HTTP 服务，实现**分段（segmented）追加日志**存储。每条记录带
**长度、全局序号、CRC-32**；写入在返回成功前 `fsync` 持久化；进程崩溃/掉电重启后：

- 已确认记录**必然保留**；
- 未确认记录**允许存在或丢失**（但绝不静默跳过损坏）；
- 只允许截去**末段末尾**不完整的尾记录；**中段损坏一律拒绝打开**；
- 序号在写数据前先持久化"认领"，**恢复后绝不复用**（因此未确认丢失会留下序号空洞）。

无界面，仅提供 HTTP/JSON 接口与 `curl` 样例。

---

## 1. 磁盘格式

一个日志 = 数据目录下的一个子目录：

```
<data_dir>/<log>/
  0000000000000000001.seg      # 段文件, 段号从 1 开始、严格连续
  0000000000000000002.seg
  ...
  next.idx                     # 序号高水位 marker (双槽原子更新)
```

### 记录帧（大端序）

```
+-----------+-----------+-----------+-----------------+
| len : u32 | seq : u64 | crc : u32 | payload [len]B  |
+-----------+-----------+-----------+-----------------+
            共 16 字节定长帧头
```

- `len`：载荷字节数（上限 64 MiB）。
- `seq`：全局单调递增序号，从 1 开始。
- `crc`：CRC-32/IEEE（与 zlib/PNG 相同，自实现，校验向量 `123456789 → 0xCBF43926`），
  覆盖 `len || seq || payload`。

记录不跨段；一条超大记录可使某段超过滚动阈值，但不会被拆开。

### 序号 marker（`next.idx`）

固定两个 24 字节槽，交替覆写，每槽：

```
version:u64 | next_seq:u64 | crc32:u32 | reserved:u32
```

### 一次追加的持久化顺序

```
1. next_seq+1 写入 next.idx 的另一槽并 fsync   ← 序号从此永久认领
2. (需要时) fsync 旧段 → 建新段 → fsync 目录
3. write_all(整帧) → fsync 段文件
4. 返回 200 + seq
```

所以每次确认追加有**两次 fsync**（序号 + 数据）。这是"连写数据前崩溃也不复用序号"
的必然代价；追求吞吐可用组提交（group commit）摊销，见文末"未完成/可改进"。

---

## 2. 崩溃恢复规则（打开时）

按段号顺序逐段严格校验：

| 情况 | 出现位置 | 处理 |
|---|---|---|
| 帧头不足 16 字节 | **仅末段末尾** | 截去该尾记录（`set_len` + fsync 文件与目录） |
| 帧头完整但声明 `len` 越过文件尾（半截载荷） | **仅末段末尾** | 截去该尾记录 |
| 上述任意一种结构性截断 | **任何已封口的中段** | **`Corrupt`，拒绝打开** |
| 帧头+声明载荷都完整却 **CRC 不符** | **任何位置（含末段末尾）** | **拒绝打开**：这是位损坏/篡改而非半截写，截断会静默丢记录 |
| `len` 荒谬（>64MiB） | 任何位置 | **拒绝打开** |
| 段号缺失/乱序、段文件名非法 | — | **拒绝打开** |
| 序号重复或回退（试图复用） | — | **拒绝打开** |
| 序号跳号（空洞） | — | **允许**：对应"认领后、落盘前崩溃"的丢失记录 |
| `next.idx` 落后于数据 / 有数据却无 marker | — | **拒绝打开** |
| `next.idx` 领先于数据 | — | 正常：差异即作废、永不复用的序号 |

中段损坏时 HTTP 层返回 `500 {"error":"log_corrupt", ...}`，该日志不可读也不可追加，
不会跳过坏记录继续服务。

---

## 3. 运行

### 依赖

- Rust（在 stable 1.98.1 上开发测试），Cargo（联网拉取 crates.io 依赖）
- Linux（恢复时对目录调用 `fsync`；段/文件名使用 `/` 路径）
- 运行时依赖：`axum 0.8`、`tokio 1`、`serde / serde_json`、`base64`
  （完整锁定见 `Cargo.lock`）

### 启动

```bash
cargo run --release
# 或先编译再运行
cargo build --release
./target/release/seglog
```

环境变量（均有默认值）：

| 变量 | 默认 | 含义 |
|---|---|---|
| `SEGLOG_DATA_DIR` | `./data` | 数据根目录，每个具名日志一个子目录 |
| `SEGLOG_SEGMENT_BYTES` | `4194304` (4 MiB) | 段滚动阈值（字节） |
| `SEGLOG_BIND` | `127.0.0.1:8080` | 监听地址 |

---

## 4. HTTP 接口与请求样例

日志名 `name` 仅允许 `[A-Za-z0-9_-]{1,128}`（防路径穿越）；不存在会自动创建。

### 4.1 健康检查

```bash
curl -s http://127.0.0.1:8080/healthz
# {"status":"ok"}
```

### 4.2 追加一条记录（原始字节）

非 JSON 请求体原样作为一条记录：

```bash
curl -s -X POST http://127.0.0.1:8080/logs/orders/records \
  -H 'Content-Type: text/plain; charset=utf-8' \
  --data 'hello, world'
# {"seq":1,"next_seq":2,"bytes":12}
```

返回 200 即表示该记录已 `fsync` 持久化。

### 4.3 追加（JSON：文本或 base64 二进制）

```bash
# UTF-8 文本
curl -s -X POST http://127.0.0.1:8080/logs/orders/records \
  -H 'Content-Type: application/json' \
  -d '{"data":"second record"}'
# {"seq":2,"next_seq":3,"bytes":13}

# 任意二进制（base64），这里是 00 01 FE
curl -s -X POST http://127.0.0.1:8080/logs/orders/records \
  -H 'Content-Type: application/json' \
  -d '{"payload_base64":"AAH+"}'
# {"seq":3,"next_seq":4,"bytes":3}
```

### 4.4 读取全部记录

```bash
curl -s http://127.0.0.1:8080/logs/orders/records
```

```json
{
  "log": "orders",
  "count": 3,
  "next_seq": 4,
  "records": [
    {"seq":1,"payload_base64":"aGVsbG8sIHdvcmxk","text":"hello, world"},
    {"seq":2,"payload_base64":"c2Vjb25kIHJlY29yZA==","text":"second record"},
    {"seq":3,"payload_base64":"AAH+","text":null}
  ]
}
```

载荷一律给 `payload_base64`；恰为合法 UTF-8 时另给 `text` 方便查看。
`next_seq` 是已认领的下一序号（可能因未确认丢失而大于 `count` 的连续期望值）。

### 4.5 日志状态

```bash
curl -s http://127.0.0.1:8080/logs/orders
# {"log":"orders","persisted_next_seq":4,"durable_last_seq":3,"record_count":3,"segments":1}
```

### 4.6 错误响应

```bash
# 请求体非法
curl -i -X POST http://127.0.0.1:8080/logs/orders/records \
  -H 'Content-Type: application/json' -d '{"nope":1}'
# HTTP/1.1 400 Bad Request
# {"error":"bad_request","message":"json body needs `data` or `payload_base64`"}

# 中段损坏（拒绝服务，不跳过）
# HTTP/1.1 500 Internal Server Error
# {"error":"log_corrupt","message":"log corrupt, open refused: segment ...: crc mismatch ..."}
```

---

## 5. 自动化测试

```bash
cargo test                 # 全部：单元测试 + 集成测试
cargo test -- --nocapture  # 查看恢复日志输出
```

测试分三层：

1. **单元测试**（`src/crc32.rs`、`src/marker.rs`、`src/log.rs`）
   - CRC 标准向量、分段续算；
   - 追加/重开、段滚动、末段半截头/半截载荷截断；
   - 中段 CRC 损坏、中段截断、段缺失、序号重复 → 拒绝打开；
   - 序号空洞允许、作废序号不复用；
   - marker 双槽交替、写一半损坏回退旧槽、全损坏拒绝。

2. **崩溃注入矩阵**（`tests/crash_recovery.rs`，真实子进程）

   测试二进制内置隐藏子命令 `crash-worker`。集成测试 fork 一个真实子进程写日志，
   在**第 3 条追加（恰好跨越段滚动）**的每个边界令进程立即死亡：

   | 注入点 | 含义 |
   |---|---|
   | `before-marker` | 认领序号前 |
   | `after-marker` | `next.idx` fsync 后、数据写前 |
   | `before-roll` / `after-roll` | 段滚动前 / 建段+目录 fsync 后 |
   | `before-data` | 数据帧 `write_all+fsync` 前 |
   | `after-data` | 数据 fsync 后、返回客户端前 |

   默认用 `_exit(9)`（不跑析构、不刷缓冲）；另有一组用 `SIGKILL` 自杀模拟掉电/OOM。
   父进程等待子进程死亡后，在同一目录重新打开，断言：

   - 打印过 `COMMITTED`（已确认）的记录**全部还在**；
   - `after-data` 的未确认记录允许保留；`after-marker/roll/before-data`
     的未确认记录允许丢失；
   - 重启后继续写入拿到的序号**全部大于**崩溃时的高水位，**从无复用**；
   - 随后把某封口段翻一个字节，重新打开必须 `OPEN_FAILED ... crc mismatch`。

3. **HTTP 端到端**（`tests/http_api.rs`）

   启动真实服务二进制（端口 0 自动分配），用原生 TCP 发 HTTP/1.1：
   raw/JSON/base64 追加与读取、重启后序号继续、非法请求 4xx，
   以及**中段损坏时 GET/POST 均返回 500 `log_corrupt` 而非跳过**。

### 手动复现崩溃注入（无需测试框架）

```bash
BIN=./target/debug/seglog
D=$(mktemp -d)

# 写 10 条，在第 3 条"数据 fsync 前"崩溃（段阈值 40B，第 3 条跨段滚动）
$BIN crash-worker --dir "$D" --seg-bytes 40 --appends 10 --crash before-data:3 ; echo "exit=$?"

# 重新打开做恢复核对
$BIN crash-worker --dump --dir "$D" --seg-bytes 40
# RECOVERED next=4 count=2 seqs=1,2      ← 1,2 已确认保留；3 已认领但数据丢失，序号留空

# 继续追加，序号从 4 开始，绝不复用 3
$BIN crash-worker --dir "$D" --seg-bytes 40 --appends 2
# COMMITTED ord=1 seq=4
# COMMITTED ord=2 seq=5
```

各注入点观察到的结果：

| 崩溃点 | 恢复后 `seqs` | `next` | 说明 |
|---|---|---|---|
| `before-marker` | `1,2` | 3 | 序号 3 尚未认领 |
| `after-marker` | `1,2` | 4 | 3 已认领、数据未写 → 作废，不复用 |
| `before-roll` / `after-roll` | `1,2` | 4 | 同上（滚动前后） |
| `before-data` | `1,2` | 4 | 帧可能写了一半 → 末段截去 |
| `after-data` | `1,2,3` | 4 | 已落盘但未确认，**允许保留** |

---

## 6. 设计取舍与边界

- **两次 fsync/追加**：序号先落盘保证"写数据前崩溃也不复用"。若允许
  "仅数据落盘后才算认领"，可降到一次 fsync，但那种实现下未确认丢失可能导致
  序号复用，不符合本任务要求。
- **空洞序号**：未确认记录丢失会在序号序列中留下洞（如 1,2,4）。这是
  "未确认允许丢失" + "序号永不复用" 的直接结果；序号仍严格递增。
- **末段截断会写盘**：恢复时把末段 `set_len` 到最后一个完整帧并 fsync，
  使截断结果本身持久化（避免每次重启重复"截一次"）。
- **单进程**：日志句柄带内存序号高水位，按单实例设计；多进程并发写同一目录
  未加锁，不在范围内。
- **页缓存/磁盘写缓存**：`fsync` 的持久性最终取决于硬件/文件系统（掉电保护盘等）；
  本服务保证到 `fsync` 成功这一层。

## 7. 未完成 / 可改进

- **未做组提交**：每次追加两次 fsync，高并发下吞吐有限；可用一次 fsync
  批量提交多个待确认请求（批次内序号仍先认领）。
- **无读取分页/游标**：`GET .../records` 一次返回全部记录，便于核对但不适合超大日志。
- **无段压缩/保留策略（compaction/TTL）**：段只增不删。
- **无鉴权/TLS**：默认仅监听 `127.0.0.1`，生产应前置网关。
- **无并发多写者支持**（文件锁）。
- 崩溃注入在进程级（`_exit`/`SIGKILL`）；未模拟真实断电下磁盘控制器重排，
  持久性边界以 `fsync` 为准。

---

## 8. 实际运行记录（2026-09-23，Linux x86_64，如实记录）

**环境**：Linux 6.8、16 核；Rust 1.98.1（stable）。本机共享工具链目录当时被其他进程
反复卸载/重装、且 `static.rust-lang.org` 访问超时，因此用
`scripts/install_toolchain.sh` 经可用镜像把工具链装进了项目内 `./.toolchain`
（该目录已 gitignore，非交付物），并用缓存的 crates.io 依赖以 `--offline` 构建。
`Cargo.lock` 中源仍为标准 `crates.io-index`，未写入任何镜像。

**构建**：

```text
cargo build --release   -> Finished release profile, 生成 target/release/seglog
```

**自动化测试 `cargo test`：31/31 全部通过。**

```text
running 20 tests  (src 单元测试)          ... test result: ok. 20 passed; 0 failed
running  5 tests  (tests/crash_recovery.rs) ... test result: ok.  5 passed; 0 failed
running  6 tests  (tests/http_api.rs)      ... test result: ok.  6 passed; 0 failed
```

**六个写入/同步边界注入崩溃后的真实恢复结果**（`--seg-bytes 40`，第 3 条 append
恰好跨段滚动）：

```text
before-marker   -> RECOVERED next=3 count=2 seqs=1,2
after-marker    -> RECOVERED next=4 count=2 seqs=1,2
before-roll     -> RECOVERED next=4 count=2 seqs=1,2
after-roll      -> RECOVERED next=4 count=2 seqs=1,2
before-data     -> RECOVERED next=4 count=2 seqs=1,2
after-data      -> RECOVERED next=4 count=3 seqs=1,2,3
```

- 已确认（打印过 `COMMITTED`）的 1、2 在所有情况下都保留；
- `after-data` 的未确认记录已 fsync 落盘 → 保留（允许存在）；
- 其余边界的未确认记录丢失，但序号 3 已被认领 → `next=4`，后续写入拿 4、5，**不复用 3**。

继续写入后的最终状态：

```text
RECOVERED next=6 count=4 seqs=1,2,4,5
[recovery] 1 sequence number(s) claimed but never durably recorded; they will not be reused
```

**损坏处理实测**：

```text
# 翻转封口段中一条记录载荷的 1 字节
OPEN_FAILED log corrupt, open refused: segment 0000000000000000001: crc mismatch at offset 0 (seq field 1)   (exit=3)

# 末段末尾追加 7 字节垃圾 (不足帧头)
[recovery] segment 0000000000000000002: discarded incomplete tail at offset 23 (partial header), kept 23 bytes
RECOVERED next=4 count=3 seqs=1,2,3   (exit=0)

# 删除第一个段 (段缺失)
OPEN_FAILED log corrupt, open refused: segment gap/disorder: expected segment 0000000000000000001, found 0000000000000000002   (exit=3)
```

**HTTP 服务 + curl 实测**：健康检查、raw/JSON/base64 三种追加、读取、状态均正常；
`kill -9` 后用同一数据目录重启，3 条记录保留、`next_seq=4`，再追加返回 `seq=4`
（未从 1 重新计数）。中段损坏日志的 GET/POST 均返回 `500 log_corrupt`，未跳过。

