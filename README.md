# cas-repo — 内容寻址块仓库（纯后端）

一个用 Rust 实现的、**文件支持（file-backed）**的内容寻址存储库，带一个本地
HTTP 验证入口。块按其内容的 SHA-256 摘要寻址、不可变；支持具名根（named
roots）、同内容去重、根更新原子发布、标记清扫（mark-sweep）垃圾回收，并在
读取/标记时用哈希做**完整性校验**。

整个项目**零第三方依赖**：SHA-256、JSON、HTTP/1.1 服务器全部手写，可离线
构建与审计。

---

## 1. 构建与运行

```bash
cargo build --release

# 启动（数据目录会自动初始化）
./target/release/cas-repo serve --data ./data --addr 127.0.0.1:8080
```

只支持一个子命令：

```text
cas-repo serve --data <DATA_DIR> [--addr 127.0.0.1:8080]
```

- `--data` / `-d`：仓库数据目录（必填）。
- `--addr` / `-a`：监听地址（默认 `127.0.0.1:8080`）。

进程对一个数据目录拥有单进程所有权（不支持两个进程同时打开同一目录）。

---

## 2. 磁盘格式（format version 1，明确且稳定）

```text
<data-dir>/
  VERSION                          # 内容固定为 "cas-repo v1\n"
  blocks/
    <aa>/                          # 摘要第 1 字节，256 个分片子目录
      <rest-62hex>                 # 不可变块：字节即块内容
      <rest-62hex>.refs.json       # 引用边车: {"version":1,"refs":["<sha256>",...]}
      .<name>.tmp-<rand>           # 原子发布的暂存文件
  roots/
    <name>.json                    # 根指针: {"hash","version","updated_at_ms"}
    .<name>.json.tmp-<rand>        # 根原子发布的暂存文件
```

格式规则：

- 块文件路径由其摘要决定：`blocks/<前2位十六进制>/<后62位十六进制>`。
- **不变性**：块字节一旦发布永不重写。再次 `put` 同摘要只做去重，并把新
  声明的引用与旧边车取并集。
- **引用边车**是独立 JSON 文件，随块一起原子更新；允许引用尚未上传的块
  （前向引用），因此 GC 能显式报告"缺失引用"。
- **根指针**是 `roots/<name>.json`，含目标块摘要、单调递增的 `version`
  和 `updated_at_ms`。根名规则：`[A-Za-z0-9._-]`、长度 1–128、不得以点开头
  （防止路径穿越和与暂存文件冲突）。
- 每次写入都走 **`写同目录暂存文件 → fsync → rename 原子替换 → fsync 目录`**。
  崩溃后可能残留 `.tmp-*` 文件；它们在**下次打开仓库时**统一回收（此刻无
  并发写者，因为是单进程所有权）。GC 运行期间**不**删除暂存文件，以免误删
  正在进行的上传。

### 哈希的定位（重要）

SHA-256 摘要**仅用于完整性校验**：

- 上传时对实际字节求摘要；若客户端在信封里给了 `hash`，必须与实算值一致，
  否则返回 `422 hash_mismatch`，块不落盘。
- 读取时重新对磁盘字节求摘要，与地址不符返回 `409 corrupt_block`。
- GC 标记每个可达块时也校验摘要，损坏块进入 `corrupt_blocks` 报告并被保留。

摘要**不是**访问凭证/能力令牌；本服务没有鉴权层，仅绑定回环地址做本地验证。

---

## 3. 同步边界（concurrency model）

单进程、多线程。一个 `Store` 对象拥有一个数据目录，内部边界如下：

| 机制 | 保护对象 |
|------|----------|
| `upload: Mutex<()>` | 串行化块/边车发布，避免同摘要并发写互相踩踏暂存文件 |
| `roots_lock: RwLock<()>` | **写者与 GC 互斥**：`put_block` 取读锁，`put_root`/`delete_root` 与 GC 取写锁；根切换、上传与 mark-sweep 永不交叠 |
| `PinTable`（引用计数） | 保护"正在读取/正在上传/GC 已标记"的块不被清扫 |

- **读块不加 `roots_lock`**：`get_block` 先 pin → 读 → 校验 → 读完 unpin。
- **清扫在 pin 表锁内执行 unlink**：并发读/上传要么其 pin 已可见（跳过删除），
  要么在删除完成后才 pin（此时观察到 not found 并可重建）。因此**可达块不
  可能被误删**。
- **根更新原子发布**：一次 temp+fsync+rename，读者只可能看到旧版本或新版本，
  不存在半个根。发布失败时旧指针完好，新块成为孤儿（下次 GC 回收）。
- 乐观并发（OCC）：`PUT /roots/{name}` 可带 `If-Match: <version>`，当前版本
  不符返回 `409 version_conflict`（不存在的根版本记为 0）。

这套设计把"引用在标记中途被加入""根在清扫中途切换"两个窗口，通过写者/GC
互斥从根上关闭；把"读到一半被删"通过 pin 在锁内删除关闭。

---

## 4. HTTP API

所有连接走真实 TCP（HTTP/1.1、keep-alive、thread-per-connection），仅用于
验证，不是生产级 Web 服务器（无 TLS、无鉴权）。

| 方法 | 路径 | 说明 |
|------|------|------|
| GET  | `/health` | 健康检查 |
| POST | `/blocks` | 上传块（原始字节，或 JSON 信封） |
| GET  | `/blocks/{hash}` | 取原始字节（响应头回带 `X-Content-Sha256`，并重新校验） |
| GET  | `/blocks/{hash}/info` | 块是否存在 + 引用列表 |
| PUT  | `/roots/{name}` | 原子发布/更新根；可选 `If-Match: version` |
| GET  | `/roots` | 列出全部根 |
| GET  | `/roots/{name}` | 读取单个根 |
| DELETE | `/roots/{name}` | 删除根（块不动，变为待回收） |
| POST | `/gc` | 执行 mark-sweep，返回报告 |
| GET  | `/stats` | 根数量、块数量、块字节数 |

### 上传格式

**原始字节**（`Content-Type: application/octet-stream`）：body 即块内容。
可用请求头 `X-Content-Sha256: <hex>` 声明期望摘要（不符则拒绝）。

**JSON 信封**（`Content-Type: application/json`）：

```json
{
  "data_b64": "<标准 base64 编码的块字节>",
  "hash":     "<可选，客户端期望的 sha256 hex>",
  "refs":     ["<可选，被引用块的 sha256 hex>", "..."]
}
```

### 发布根

body 可以是 JSON `{"hash":"<hex>"}`，也可以直接放裸摘要字符串。
目标块必须已存在，否则 `404 not_found`。

### GC 报告

```json
{
  "roots_scanned": 2,
  "blocks_before": 4,
  "blocks_live": 4,
  "blocks_removed": 1,
  "bytes_removed": 34,
  "removed": ["<hex>", "..."],
  "missing_references": [ { "parent": "<hex 或 null>", "missing": "<hex>" } ],
  "corrupt_blocks": ["<hex>"],
  "warnings": []
}
```

- 从所有根出发沿 `refs` 做可达性遍历；只清扫**不可达且未被 pin** 的块。
- `missing_references`：父块（`parent=null` 表示根目标本身缺失）引用了磁盘上
  不存在的摘要。父块本身可达时**保留**，只报告。
- `corrupt_blocks`：可达但字节与地址摘要不符。保留并报告，绝不静默删除。

完整可复制的请求样例见 [`examples/requests.http`](examples/requests.http)
和可直接运行的 [`scripts/demo.sh`](scripts/demo.sh)。

---

## 5. 可注入 I/O 层与故障模拟

所有磁盘访问都经过 `vfs::Vfs` trait（`src/vfs.rs`）：

- `StdVfs`：`std::fs` 的薄封装，真实服务器使用。
- `FaultyVfs`：包一层，在具名**调用点（site）**上注入故障。故障规则可在
  运行时装填（`FaultMap::arm / fail_once / clear`），跨线程共享。

故障动作（`FaultAction`）：

- `Fail`：该调用直接返回注入的 `io::Error`，什么都不做。
- `CorruptByte { offset, xor }`：写暂存文件前把指定字节异或，制造磁盘损坏。
- `SucceedThenFail`：操作实际成功但仍返回错误（"写成功却报错"）。

关键注入点：`rename_root`（根发布 rename 前崩溃）、`rename_block`、
`rename_block_tmp`、`remove_block`、`read_block`、`exists_block` 等。

示例（Rust，见 `tests/store_tests.rs`）：

```rust
let faults = FaultMap::new();
let vfs = FaultyVfs::new(Box::new(StdVfs::new()), faults.clone());
let store = DynStore::open_boxed(dir, Box::new(vfs))?;

let a = store.put_block(b"v1", None, &[])?.hash;
store.put_root("main", a, None)?;
let b = store.put_block(b"v2", None, &[])?.hash;

faults.fail_once("rename_root");           // 根发布 rename 时"崩溃"
assert!(store.put_root("main", b, None).is_err());
assert_eq!(store.get_root("main")?.hash, a);   // 旧根完好
assert!(store.block_exists(&b)?);              // 新块成为孤儿
let report = store.gc()?;                      // 孤儿被回收，可达块不动
```

---

## 6. 自动化测试

```bash
cargo test              # 全部
cargo test --release    # 优化构建下再跑一遍
cargo clippy --all-targets
```

测试组成：

- `src/hash.rs`：SHA-256 NIST 向量（含一百万个 'a'）、跨块边界流式一致性。
- `src/json.rs`：往返、Unicode 转义、尾随垃圾拒绝。
- `tests/store_tests.rs`（19 个）：
  - 去重、声明哈希校验、根发布与 OCC、非法根名；
  - **孤儿块回收 / 可达块保留**；
  - **缺失引用**（父块引用幽灵块、根目标缺失）；
  - **pin 保护**、读与 GC 并发、根切换与 GC 竞态、上传与 GC 竞态；
  - **同块 16 线程并发上传**（恰好一个非去重）、8×10 个不同块并发上传；
  - 故障注入：**根切换中断**（旧根保留、新块孤儿、残留暂存文件重开清理）、
    块写失败不落盘、损坏块读取拒绝/GC 报告保留、GC 删除 I/O 失败中止。
  - 重开持久化、格式版本不匹配拒绝。
- `tests/http_tests.rs`（8 个）：真实 TCP 端到端，覆盖健康检查、原始/JSON
  上传、去重、下载校验、哈希不符 422、根生命周期与 OCC、GC 端点、
  **12 个 HTTP 客户端并发上传同块**、404。

---

## 7. 实际运行记录（本次交付环境）

环境：Linux 6.8.0，`rustc 1.98.1` / `cargo 1.98.1`。

### 7.1 单元/集成测试

```text
$ cargo test
running 6 tests   ... src/lib.rs        6 passed; 0 failed
running 8 tests   ... tests/http_tests.rs   8 passed; 0 failed
running 19 tests  ... tests/store_tests.rs 19 passed; 0 failed
Doc-tests cas-repo: 0 passed; 0 failed
```

`cargo test --release` 同样全部通过；`tests/store_tests.rs` 连续重复运行
20 次全部通过（压测并发用例的稳定性）。`cargo clippy --all-targets` 无
警告。

### 7.2 真实二进制端到端

`./scripts/demo.sh`（退出码 0）验证：两块上传 → 重复上传 `deduplicated:true`
→ 带引用的 manifest 上传 → 块 info → 原子发布根 `main` → 下载回读 →
哈希不符被 `422` 拒绝 → 造孤儿块后 `POST /gc` 回收
（`blocks_removed:1`）→ 造缺失引用，GC 在 `missing_references` 中报告且
保留父块 → **8 个并发客户端上传同块**，返回同一个摘要且恰有一个
`deduplicated:false` → 最终 `stats {blocks:5, roots:2}`。

另手工验证（输出见提交时的会话记录）：

- 直接篡改磁盘块首字节后 `GET /blocks/{hash}` 返回
  `409 corrupt_block`（哈希完整性校验生效）。
- 模拟"根发布 rename 前崩溃"（留下 `roots/.main.json.tmp-deadbeef`），
  重启后暂存文件被回收，旧根 `main` 仍完整可读（原子发布语义）。

### 7.3 已知限制 / 未覆盖项（如实说明）

- 无鉴权、无 TLS；HTTP 服务器为 thread-per-connection，仅用于本地验证。
- 不支持多进程同时打开同一数据目录（单进程所有权）。
- GC 报告 HTTP 状态恒为 200，缺失引用/损坏通过 JSON 字段表达（未用 207
  多状态码），由调用方检查 `missing_references`/`corrupt_blocks`。
- 块大小上限固定为 32 MiB（`DEFAULT_MAX_BLOCK_BYTES`），暂未做成配置项。
- 未做真实磁盘 fsync 掉电测试（需要故障注入的 FUSE/故障设备）；持久性
  保证依赖 `write + fsync + rename + fsync(dir)` 的 POSIX 语义，通过
  `FaultyVfs` 在调用点模拟失败窗口。

---

## 8. 目录结构

```text
Cargo.toml              # 零依赖
src/
  main.rs               # CLI: cas-repo serve --data ... --addr ...
  lib.rs
  hash.rs               # 手写 SHA-256（仅完整性校验）
  json.rs               # 手写 JSON 解析/序列化
  vfs.rs                # Vfs trait + StdVfs + FaultyVfs（故障注入）
  store.rs              # 块/根存储、pin 表、mark-sweep GC、磁盘格式
  server.rs             # 手写 HTTP/1.1 验证入口
tests/
  common/mod.rs         # 临时目录、base64 测试辅助
  store_tests.rs        # 仓库层 + 故障注入 + 并发验收（19）
  http_tests.rs         # 真实 TCP 端到端（8）
scripts/demo.sh         # 一键端到端演示
examples/requests.http  # 可复制的 HTTP 请求样例
README.md
```
