# 增量 Merkle 校验（incremental-merkle / imerkle）

纯后端 Rust 项目：文件支持的分块存储库 + 本地 HTTP 验证入口。实现分块文件的 Merkle
树与**范围证明**，更新少量块时仅重算受影响路径；验证者只读取证明内携带的块，
**不读取完整文件**。存储层有明确的磁盘格式与崩溃同步边界（WAL），并通过可注入的
I/O 层模拟 I/O 错误与断电崩溃。

无前端。仅依赖 `sha2` / `serde` / `serde_json`，HTTP 服务用标准库 `std::net` 手写。

---

## 1. Merkle 哈希规范（参与方必须一致）

底层哈希为 **SHA-256**（32 字节）。所有多字节整数为**小端**编码。

| 对象 | 摘要计算 |
|---|---|
| 空文件根（0 块） | `SHA256("MERKLE_EMPTY_ROOT_v1" || u64-le(chunk_size))` |
| 叶节点（块 `i`） | `SHA256(0x00 ‖ u64-le(i) ‖ u64-le(chunk_size) ‖ chunk_bytes)` |
| 内部双亲（level L≥1） | `SHA256(0x01 ‖ u64-le(L) ‖ left_hash ‖ right_hash)` |

- **域分离**：叶前缀 `0x00`、内部前缀 `0x01`，二者不可能互相伪造；内部节点还绑定 `level`。
- **位置绑定**：块序号 `i` 进入叶摘要，块不能在证明中被重排（防错位证明）。
- **参数绑定**：`chunk_size` 进入空根与每个叶，防止不同分块参数混用。
- **奇数节点规则（odd promotion）**：自底向上归约时，一层节点两两配对
  `(0,1),(2,3),…`；若该层节点数为奇数，**最后一个节点不复制、不哈希，原样提升**到
  上一层同位置。层宽序列示例：5 叶 → 5,3,2,1。

> 选择“提升（promotion）”而非比特币式“复制最后一个节点（duplication）”，避免
> `H(leaf,leaf)` 这类可被叶摘要碰撞利用的结构。

### 增量更新

修改若干块后，只把新叶写入叶层，然后自底向上**仅重算覆盖到脏孩子的双亲**；
奇数提升位与未涉及的子树保持不变。同形树（块数不变）走该增量路径；块数增长/缩短
导致树形变时全量重建（形变本身已无法保证路径不变）。实现见
`src/merkle.rs::update_levels`。

### 范围证明（range proof）

对连续块区间 `[start, end)` 生成包含证明：

- `chunks`：**仅区间内**块的原始数据（hex）。验证者据此重算叶，无需完整文件。
- `steps`：从叶层到根、每层至多一个左兄弟哈希、一个右兄弟哈希，外加一位 `promoted`
  （该步切片末尾是否含本层奇数提升位——提升状态会随区间向上传播，验证者无法仅凭层宽
  推断，故显式携带；伪造该位只会导致最终根不匹配）。

验证（`RangeProof::verify`，无状态、无外部数据）依次检查：

1. `chunk_size > 0`；
2. 区间合法（非空、不越界）；
3. **长度一致性**：`chunk_count(file_length, chunk_size) == total_chunks` —— 防伪造长度；
4. 携带块数 == `end - start`；步数 == 树高；
5. 逐块重算叶（带位置），按全局奇偶对齐逐层归约（提升位原样上移）到根；
6. 重算根 == 证明承诺根。

---

## 2. 磁盘格式与同步边界

仓库根目录布局：

```text
<repo>/
├── manifest.json        # 已提交状态快照（原子 rename 发布）
├── journal              # 前滚 WAL：帧序列
├── chunks/<i>           # 正式块，文件名为块序号，内容为原始块字节
└── tmp/staging/<i>      # 提交暂存块
```

`manifest.json`：

```json
{ "version": 1, "chunk_size": 16, "file_length": 50,
  "chunk_count": 4, "root": "<64 hex>" }
```

`journal` 帧：

```text
偏移  长度  内容
0     8    魔数 "IMRKJRN1"
8     4    payload 长度 n（小端 u32）
12    n    payload（UTF-8 JSON：version/chunk_size/file_length/chunk_count/changes/deletes/root）
12+n  32   SHA256(payload)
```

### 提交流程（每步对应一个可注入故障点 `FaultPoint`）

1. 写暂存块 `tmp/staging/<i>` 并逐块 `fsync`（`ChunkWrite`/`ChunkSync`）；
2. 追加 journal 提交帧并 `fsync` journal（`JournalAppend`/`JournalSync`）——
   **提交点（commit barrier）**：此 fsync 成功返回后，即使断电，恢复也必须到达新状态；
3. 暂存块原子 rename 提升为 `chunks/<i>`，删除收缩的尾块（`PromoteChunk`）；
4. 写 `manifest.json.tmp` → `fsync` → 原子 rename 为 `manifest.json`
   （`ManifestWrite`/`ManifestSync`/`ManifestRename`）——检查点；
5. journal 截断为 0 并 `fsync`，清理暂存残留（`JournalTruncate`/`JournalTruncateSync`）。

### 崩溃恢复（打开时）

以 `manifest.json` 为基线，顺序解析 journal 帧：**第一个截断/校验失败的帧即停止**，
尾部丢弃（torn write 不会生效）；version 更新的有效帧幂等前滚（暂存块缺失但正式块
已存在视为已提升），随后重建检查点并清空 journal。块与最终根做全量一致性自检，
不符则报“corrupt repository”。

同步边界的精确定义：`JournalSync` 的 fsync **未成功返回**即视为未提交（保守）；
成功返回后任何阶段崩溃，恢复结果都是新状态。

---

## 3. 可注入 I/O 层

存储库只依赖 `src/vfs.rs` 的 `Vfs` trait：

- `RealVfs`：`std::fs` 实现，真实 fsync / rename / set_len 语义；
- `MemVfs`：内存文件系统，记录每个文件“最后一次 sync 的长度”，`simulate_crash()`
  把文件截断回该长度并删除从未 sync 的文件（模拟掉电丢未持久化写入）；rename 会把
  源文件的持久化长度转移给目标。

`FaultPolicy` 可在 10 个故障点之第 N 次命中时注入：

- `io_error(...)`：返回 I/O 错误但不丢数据（进程存活）；
- `crash(...)`：返回错误并触发 `simulate_crash`（断电）。

测试对**全部 10 个故障点**分别注入错误与崩溃，断言重开后仓库原子、自洽
（见 `store::tests`）。

---

## 4. 构建与测试

```bash
cargo build --release
cargo test                 # 29 个测试（含 11480 个区间的穷举归约被收敛为 n≤30 的快速版）
cargo run --release -- serve --addr 127.0.0.1:8099 --data-dir ./data
```

---

## 5. HTTP 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/repos/:name/open?chunk_size=4096` | 创建/打开仓库（幂等） |
| GET  | `/repos/:name` | 状态：`root`、`file_length`、`chunk_count`、`version` |
| PUT  | `/repos/:name/file` | 替换文件；body 为 `{"data":"<hex>"}` 或裸 hex |
| GET  | `/repos/:name/proof?start=&end=` | 生成范围证明（`end` 默认到末尾） |
| POST | `/verify` | **无状态**校验任意 RangeProof JSON，不依赖任何仓库/文件 |

完整可运行示例见 [`requests/http-requests.md`](requests/http-requests.md) 与
[`requests/curl-demo.sh`](requests/curl-demo.sh)。

证明 JSON 示例（节选）：

```json
{
  "chunk_size": 16,
  "file_length": 50,
  "total_chunks": 4,
  "start_chunk": 0,
  "end_chunk": 1,
  "root": "6bd3201f…",
  "chunks": ["000102…0f"],
  "steps": [
    { "right": "…" },
    { "right": "…", "promoted": false }
  ]
}
```

`POST /verify` 返回 `{"valid":true}` 或 `{"valid":false,"error":"<原因>"}`。

### 离线验证语义说明

验证者只证明“证明携带的块 ↔ 证明自带的承诺根”自洽，它不知道服务器的最新根。
要确认数据属于**最新版本**，校验方需另行取得当前 `GET /repos/:name` 的 `root`
并确认与证明中的 `root` 一致。用“新根 + 旧块”验证会得到 `RootMismatch`。

---

## 6. 源码结构

| 文件 | 职责 |
|---|---|
| `src/merkle.rs` | 分块、域分离哈希、奇数提升、增量 `update_levels`、范围证明生成/验证 |
| `src/vfs.rs` | `Vfs`/`VfsFile` trait、`RealVfs`、`MemVfs`（含崩溃模拟）、`FaultPolicy` |
| `src/store.rs` | 磁盘布局、WAL 提交/重放、`Repository`（put_file/range_proof/verify_block_store） |
| `src/http.rs` | 标准库 HTTP 服务、路由、无状态 `/verify` |
| `src/main.rs` | `imerkle serve` CLI |
| `src/hex_codec.rs` | hex 编解码 |
