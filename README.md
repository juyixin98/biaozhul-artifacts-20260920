# merkle-store — 增量 Merkle 校验的分块文件存储（纯后端）

一个零外部依赖的 Rust 后端项目：把文件切成固定大小的块，维护一棵增量更新的
Merkle 树（叶/内部节点域分离、奇数节点上提、不复用不复制），并提供本地
HTTP 验证入口。验证者**只读取被请求的块和 O(log n) 个证明节点，不读取完整文件**。
存储的每一次写事务都有明确的落盘/同步边界，可用注入式 I/O 层模拟崩溃与撕裂写。

无前端。所有加密（SHA-256）、base64、JSON、HTTP/1.1 均在仓库内手写实现。

---

## 1. Merkle 规范

块大小 `block_size`，文件长度 `data_len`，块数 `n = ceil(data_len / block_size)`
（空文件 `n = 0`）。

- **叶节点**（提交块 `i` 的精确字节 `b`）：

  ```
  LEAF(i,b) = SHA256( 0x00 || i:u64be || len(b):u64be || b )
  ```

  块下标与块长度都进入哈希，因此块不能被重排，最后一个短块也不能被填充来伪造文件长度。

- **内部节点**：

  ```
  H(L,R) = SHA256( 0x01 || L || R )
  ```

  `0x00`/`0x01` 域分隔标签使叶哈希与内部哈希永不冲突。

- **奇数节点规则**：某层节点数为奇数时，最后一个节点**原样上提**到上一层，
  不与自身配对、不复制（Certificate Transparency 风格）。

- **空文件根**：零叶树的根为固定哨兵
  `SHA256("merkle-store:empty-root-v1")`，按构造与任何叶/内部哈希不同。

### 范围证明

对块区间 `[start, end)` 的证明包含：

1. 区间内每个块的叶节点（验证者用实际块字节**重算**，不信任证明里的叶哈希）；
2. 逐层重建根所需的兄弟/子树哈希，每个节点都带 `(level, index)` 定位标签。

验证时按规范要求的 `(level,index)` 顺序逐个消费证明节点：**错位证明**（交换、
重排、伪造标签）在比较哈希之前就会因位置不符而被拒绝（`MisalignedNode`）。
证明被截断或有多余节点分别报 `ProofTooShort` / `ProofTooLong`。

### 长度绑定与伪造防护

验证请求必须同时给出 `block_size`、`data_len`、`n`。验证者首先检查
`n == ceil(data_len/block_size)`，并按块下标重算每个块应有的长度
（非末块必须等于 `block_size`，末块等于 `data_len - (n-1)*block_size`）。
因此：

- 声称与块数不一致的文件长度 → `LengthMismatch`；
- 给短末块补零到整块 → 叶哈希不符 → `LeafMismatch`；
- 根对不上 → `RootMismatch`；
- 空文件但根不是空哨兵 → `EmptyRootMismatch`。

完整错误枚举见 `src/merkle.rs` 的 `ProofError`。

---

## 2. 磁盘格式与同步边界

每个存储库是一个目录（所有整数大端序）：

```
<repo>/
  manifest      64 字节固定格式（见下）
  journal       仅在写事务提交过程中短暂存在
  data.bin      原始文件字节
  level-0.bin   n      * 32 字节 —— 叶哈希
  level-1.bin   ⌈n/2⌉  * 32 字节
  ...           每层一个文件，level-h.bin 只有 32 字节（根）
```

### manifest（64 字节）

| 偏移 | 长度 | 字段 |
|---|---|---|
| 0  | 16 | magic `MS-MANIFEST-V1\0\0` |
| 16 | 8  | block_size (u64) |
| 24 | 8  | data_len (u64) |
| 32 | 8  | n (u64) |
| 40 | 24 | 保留，必须为 0 |

打开时强校验 magic、长度、保留字段以及 `n == ceil(data_len/block_size)`。

### journal

```
magic "MS-JOURNAL-V1\0\0\0" (16)，随后若干条目：
  op:u8 (1=put / 2=reset) | index:u64be | len:u64be | data(len 字节)
```

- `PUT /block`：日志含单条 put（块下标 + 块字节）。
- `POST /build`（reset）：日志含单条 reset，携带整个新载荷（**包括空载荷**，
  这样“崩溃在日志落盘后、数据更新前”不会被误判为无事务）。

### 每次写请求的同步边界

1. **journal 原子落盘**：写临时文件 → `fsync` → `rename` → 目录 `fsync`；
2. `data.bin` 定点修补（`pwrite` + `fsync`，仅末块缩短时 `truncate`）；
3. Merkle 层**沿受影响路径逐层增量重算**，每层一次定点写 + `fsync`；
4. 新 manifest 原子发布 → 删除 journal。

第 1 步完成后、第 4 步完成前的任意崩溃，在下次 `open` 时通过重放 journal 恢复：
按日志把 `data.bin` 修补到位，再从 `data.bin` **全量重建所有层**——
无论崩溃时层文件是否已被部分修补，结果都一致。

`RealVfs` 用真实的临时文件+fsync+rename；`MemVfs` 是内存文件系统；
`FaultyVfs<V>` 包裹任意 VFS，可在“第 N 次匹配调用”注入 I/O 错误或翻转首字节
（撕裂写），见 `src/vfs.rs`。

---

## 3. HTTP API（本地验证入口）

每连接一个请求（`Connection: close`），原生 HTTP/1.1，默认监听 `127.0.0.1:8080`。

| 方法与路径 | Body | 说明 |
|---|---|---|
| `GET /health` | — | 存活检查 |
| `GET /root` | — | block_size / data_len / n / root(base64+hex) |
| `GET /range?start=S&end=E` | — | 区间 `[S,E)` 的块 + 证明 |
| `GET /range?which=first\|last` | — | 首块 / 末块（含短块）；空文件给 `[0,0)` |
| `PUT /block?index=I` | 原始字节 | 增量写单块；`I==n` 为追加 |
| `POST /build` | 原始字节 | 日志保护的整文件原子重建 |
| `POST /verify` | JSON | **无状态**：只用请求体内的块与证明，绝不读取存储库 |

`/range` 的响应体本身就是合法的 `/verify` 请求体（已内含受信 `root`）。
`/verify` 也可由另一个进程 / 另一个空存储库提供——它与被验证的存储完全解耦。

更多 curl 样例见 [`examples/http-requests.md`](examples/http-requests.md)，
可直接喂给 `verify` 的 JSON 见 `examples/verify-*.json`。

### 块追加约束（无空洞）

块是字节流的连续划分，除末块外每块恰为 `block_size` 字节。只有当当前末块已满
（`data_len` 是 `block_size` 的整数倍）时才允许追加新块；否则需要先把末块写满。
在短末块后尝试追加会返回 HTTP 400。

---

## 4. 构建、测试、运行

需要 Rust（开发用版本 1.98.1，edition 2021，无第三方依赖）。

```bash
cargo build --release
cargo test                 # 全部自动化测试
cargo clippy --all-targets # 零警告
bash scripts/demo.sh       # 端到端可复现演示（自动起停两个本地服务）
```

CLI：

```bash
./target/release/merkle-store init  /tmp/repo --block-size 4
printf 'abcdefghijk' > p.bin
./target/release/merkle-store build /tmp/repo --file p.bin
./target/release/merkle-store root  /tmp/repo
./target/release/merkle-store range /tmp/repo --which first > first.json
./target/release/merkle-store verify first.json      # 退出码 0=有效，1=无效
./target/release/merkle-store serve /tmp/repo --addr 127.0.0.1:8080
```

### 模块地图

| 文件 | 职责 |
|---|---|
| `src/sha256.rs` | SHA-256（FIPS 180-4，含标准测试向量） |
| `src/base64.rs` | RFC 4648 base64 |
| `src/json.rs` | 极简 JSON 解析/序列化 |
| `src/merkle.rs` | 树构建、**增量无关的**范围证明生成与验证 |
| `src/vfs.rs` | 可注入 I/O：`RealVfs` / `MemVfs` / `FaultyVfs` |
| `src/store.rs` | 磁盘格式、journal、同步边界、增量路径重算、崩溃恢复 |
| `src/http.rs` | 手写 HTTP/1.1 服务端 |
| `src/api.rs` | 路由；`/verify` 无状态验证逻辑 |
| `src/main.rs` | CLI（含离线验证器与原生 TCP 请求子命令） |
| `tests/acceptance.rs` | 验收矩阵（真实磁盘） |
| `tests/http_api.rs` | HTTP 端到端（独立验证者进程语义） |

---

## 5. 验收项与结果

自动化测试在本机实际运行通过（详细命令与输出见 [`RUNLOG.md`](RUNLOG.md)）：

- **与全量重建比较根**：逐块追加 + 覆盖首/中/末块（末块可缩短/恢复），
  每次增量根都等于对当前字节做全量重建的根；另设 `force_full_rebuild` 交叉校验。
- **验证首尾范围**：首块、末块（短块）及内部区间证明均验证成功，
  验证者只持有被请求块的字节（测试中显式只传这些块）。
- **空文件**：根为空哨兵；空证明 `[0,0)` 对哨兵验证成功，对其他根报 `EmptyRootMismatch`；
  有数据后 reset 回空恢复哨兵。
- **伪造长度**：`data_len` 与 `n/block_size` 不符 → `LengthMismatch`；
  短末块补零 → `LeafMismatch`；错误受信根 → `RootMismatch`。
- **错位证明**：交换兄弟节点、伪造 index 标签 → `MisalignedNode`；
  截断/追加节点 → `ProofTooShort`/`ProofTooLong`。
- **增量成本**：256 叶树改 1 个块，恰好每层一次 32 字节写（9 层 9 次），
  `data.bin` 恰好一次定点写，证明更新为单条根路径。
- **故障注入**：在一次追加事务的每个落盘步骤注入失败，重开后存储内部一致
  （根 == 对现存 `data.bin` 的全量重建）；journal 存活时撕裂的层字节被重放修复。
- **验证者不读完整文件**：`tests/http_api.rs` 用一个以**空内存 FS** 支撑的独立服务
  成功验证来自另一个真实磁盘存储库的证明；`/verify` 代码路径不接触被验证存储。

统计：`cargo test` 共 **43** 个测试（32 单元 + 8 验收 + 3 HTTP），全部通过；
`cargo clippy --all-targets` 无警告。

## 6. 安全边界与非目标

- 受信锚点是根哈希；`/verify` 不负责“取得”受信根，只负责证明与受信根一致。
- 这是本地单进程教学/验证型存储：写路径用互斥锁串行化，未做并发写优化、
  鉴权、TLS 或远程复制。
- journal 只保证单次 put/reset 的原子性与崩溃恢复，不是通用预写日志（无多语句事务）。
