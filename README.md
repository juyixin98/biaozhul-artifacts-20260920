# 双页超级块恢复（dual-sb-recovery）

纯 Rust、**零第三方依赖**的追加式键值存储：数据文件追加记录，根节点写入
**两个交替的超级块**之一，带代次（generation）、根偏移和 CRC-32 校验。
通过一个**可注入的 I/O 层**在每次写入/同步前后模拟断电（含半页撕裂写），
验证「先同步数据、再发布根」的崩溃安全提交协议。另附一个仅用标准库实现的
本地 HTTP 验证入口。无前端。

---

## 1. 它解决什么问题

单个根节点（元数据页）在写入过程中断电会产生撕裂页，可能让存储看到一个
「半新半旧」的根。本项目用两块固定的超级块页交替发布：

- 提交 N 时，先把新数据记录落盘并 `fsync`，**之后**才把指向它的新超级块
  写到与上一个根不同的槽位并 `fsync`；
- 任何时刻至少有一个槽位保存着上一个完整、已校验的根；
- 恢复时校验两个槽位，**在「完整且其引用链有效」的候选中选取代次最高者**，
  损坏页（CRC 错、魔数错、指针越界、引用链断裂）一律跳过；
- 两个槽位都无效时，明确拒绝打开（`NoValidSuperblock`），绝不接受坏根；
- 数据文件尾部未被任何有效根引用的「孤儿/撕裂」字节在打开时被截掉。

---

## 2. 磁盘格式

仓库就是一个目录，含三个文件（小端字节序，校验均为 CRC-32/ISO-HDLC）：

| 文件 | 作用 |
|------|------|
| `data.log` | 追加式记录日志，记录从偏移 0 紧凑排列 |
| `sb.a` | 超级块槽位 A，存在时恰为 4096 字节 |
| `sb.b` | 超级块槽位 B，存在时恰为 4096 字节 |

### 2.1 超级块（固定 4096 字节 = 一页）

```
偏移   长度  字段
0      4    魔数 b"DSB1"
4      4    格式版本（u32，当前为 1）
8      8    代次 generation（u64，从 1 起，每次提交 +1）
16     8    data_len：data.log 的逻辑高水位（u64）
24     8    root_off：当前链头记录在 data.log 中的绝对偏移（u64）
32     4    槽位：0=sb.a，1=sb.b
36     4036 保留，必须为 0
4072   4    对字节 [0,4072) 的 CRC-32
4076   20   尾部保留，必须为 0
```

半页写会破坏 CRC（或使保留区非零），整页被拒绝。

### 2.2 数据记录

```
偏移  长度  字段
0     4    魔数 b"RECD"
4     4    载荷长度 L（u32）
8     4    prev_off：上一条记录的绝对偏移；链首为 NIL=0xFFFFFFFF
12    8    写入该记录的提交代次（u64）
20    8    record_off：本条记录自身的绝对偏移（u64，用于位置自检）
28    8    保留，必须为 0
36    4    对字节 [0,36) 的 CRC-32
40    L    载荷
```

记录总长 `40 + L`。链头记录的代次必须等于超级块代次；更旧的记录保留各自
写入时的代次。记录紧凑排列，因此第 i 条记录结束偏移恰为第 i+1 条的偏移。

### 2.3 载荷（一次提交可含多个操作）

```
0  1  操作：1=put，2=delete
1  4  key 长度 K（u32）
5  4  value 长度 V（u32，delete 为 0）
9  K  key（UTF-8 字节）
9+K V  value（仅 put）
```

> 偏移字段 `prev_off` 为 u32：单数据文件的逻辑上限约 4 GiB（本工具为本地
> 验证用途，足够；格式中已用 NIL 哨兵避免「偏移 0」与「无前驱」歧义）。

---

## 3. 同步边界（提交协议）

一次提交恰好追加一条记录、发布一个新根，顺序严格如下：

```
1. pwrite(data.log, 尾部, 记录)     // 新数据先进入页缓存
2. fsync(data.log)                  // 屏障：记录已持久
3. pwrite(另一槽位, 0, 超级块页)     // 根只引用已持久的记录
   fsync(目录)                      // 仅首次提交：固化新建目录项
4. fsync(该槽位)                    // 根持久 —— 提交在此点变为可见
```

关键是 **2 在 3 之前**：根绝不会引用尚未落盘的数据。

| 崩溃位置 | 后果 | 恢复结果 |
|----------|------|----------|
| 步骤 1–2（数据写/同步前后） | 旧根完好；新数据缺失或只是尾部撕裂前缀 | 回到上一代，撕裂尾部被截断 |
| 步骤 3–4（新根写/同步前后） | 数据已在；新根页撕裂，CRC 失败 | 另一槽位的上一代根胜出 |
| 步骤 4 返回后 | 新根已完整持久 | 新一代可见（崩溃错误仍返回，但数据已落盘） |

---

## 4. 恢复选取算法

1. 读取 `data.log` 长度 `file_len`；读取两个 4096 字节超级块。
2. 对每页做：长度/魔数/版本校验 → CRC → 保留区清零 → 槽位合法 →
   代次非零 → `data_len <= file_len` 且 `root_off < data_len`（**越界拒绝**）。
3. 对通过的页，沿 `root_off → prev_off → …` 走引用链并完整校验：
   - 每条记录头 CRC、魔数、`record_off` 自检；
   - 头记录代次 == 超级块代次；
   - 链必须回走到偏移 0、记录从 0 起紧凑排列、链尾恰在 `data_len`；
   - 每条载荷可解码。
4. 在所有「页有效 **且** 引用链有效」的候选中，取**代次最高者**
   （同代次取槽位号更大者）。
5. 选中链之后，`file_len` 超出 `data_len` 的部分是孤儿/撕裂尾部，
   `truncate + fsync` 截掉；随后重放操作重建内存 KV。
6. 若存在非空超级块但没有任何候选，返回 `NoValidSuperblock`，拒绝打开。

---

## 5. 可注入 I/O 层与故障模型

存储库只依赖 `src/io.rs` 中的 `Vfs` trait（`pwrite / sync / sync_dir /
read / truncate`）。两个实现：

- **`RealVfs`**：真实目录，`pwrite` + `fsync`，HTTP/CLI 使用。
- **`SimVfs`**：内存介质，模拟带页缓存的操作系统：
  - `pwrite` 只进入脏层；`sync` 才把脏段并入「持久介质」；
  - `CrashPolicy` 可在 8 个 `CrashPoint` 之一**一次性**断电：
    数据写开始/结束、数据同步开始/结束、根写开始/结束、根同步开始/结束；
  - `Torn` 控制崩溃时在途写入的存活形态：`None`（全丢）、
    `Half`（恰好前半页）、`Short`（短前缀）；
  - 断电会清空全部脏层（等价于掉电后冷页缓存）；对**同一份持久介质**
    调用 `reopen` 即「重启」，恢复代码看到的就是掉电后真实存活的字节。

> 故障策略只在目标提交**打开仓库之后**武装，因此恢复过程自身可能做的
> 截断/同步不会误触发注入的崩溃。

---

## 6. 构建与运行

需要 Rust（在 `rustc 1.98.1` 上开发）。无外部依赖。

```bash
cargo build --release
./target/release/dual-sb help
```

### 6.1 命令行（真实文件）

```bash
BIN=./target/release/dual-sb

$BIN init   --dir ./mystore
$BIN put    --dir ./mystore --key name --value alice
$BIN get    --dir ./mystore --key name
$BIN put    --dir ./mystore --key name --value bob   # 覆盖 -> 新一代
$BIN delete --dir ./mystore --key name
$BIN list   --dir ./mystore
```

每次打开都会打印恢复报告（选中槽位/代次、截断的孤儿字节数、每个槽位的
接受或拒绝原因）。

### 6.2 崩溃注入演示（内存模拟，不碰真实目录）

```bash
# 提交第 3 代时，在 8 个点 × 3 种撕裂形态 = 24 个用例上逐一断电后恢复
$BIN demo matrix

# 提交第 2 代（第一次回退）时，8 个点、半页撕裂
$BIN demo matrix2

# 三个指定验收场景：半页根写 / 两个根均损坏 / 新高代次指针越界回退旧根
$BIN demo scenarios

# 单点复现
$BIN demo crash --point RootWriteEnd --torn Half --round 3
```

### 6.3 本地 HTTP 验证入口

```bash
$BIN serve --dir ./mystore --addr 127.0.0.1:8080
```

| 方法 | 路径 | 说明 |
|------|------|------|
| GET  | `/health` | 健康检查 |
| GET  | `/kv` | 列出全部条目及当前代次/data_len |
| GET  | `/kv/get?key=K` | 读取单个键 |
| POST | `/kv/put` | JSON `{"key":..,"value":..}`，一次提交 |
| POST | `/kv/delete` | JSON `{"key":..}` 或 `?key=`，一次提交 |
| GET  | `/demo/matrix` | 24 用例崩溃矩阵（内存模拟） |
| GET  | `/demo/matrix2` | 第 2 代崩溃、半页撕裂矩阵 |
| POST | `/demo/crash` | `{"point","torn","round"}` 单点崩溃 |
| GET  | `/demo/scenarios` | 三个命名验收场景的详细报告 |

`/kv*` 操作真实目录；`/demo*` 完全在内存模拟器中运行，互不影响。
可直接用 [`examples/requests.sh`](examples/requests.sh) 或
[`examples/requests.http`](examples/requests.http) 发起请求。

---

## 7. 测试

```bash
cargo test
```

- `src/**` 单元测试：CRC 已知向量、超级块/记录编解码、撕裂页检测、
  模拟器掉电语义。
- `tests/persistence.rs`：**真实文件**后端的重开持久化、磁盘文件形态、
  孤儿尾部截断、越界根拒绝。
- `tests/crash_recovery.rs`：完整崩溃矩阵（8 点 × 3 撕裂）、首次回退矩阵、
  半页超级块回退、两个根均损坏拒绝、新高代次指针越界回退、引用记录 CRC
  损坏回退、撕裂数据记录截断、未同步脏写丢失、槽位交替与代次单调。

实际运行记录见 [`RUNLOG.md`](RUNLOG.md)（命令、结果、曾失败并修复的项，
如实记录）。

---

## 8. 源码结构

```
Cargo.toml
src/
  main.rs     CLI 入口（serve / init / put / get / delete / list / demo）
  lib.rs      模块声明，#![forbid(unsafe_code)]
  crc32.rs    零依赖 CRC-32（常量表编译期生成）
  format.rs   磁盘格式：超级块、记录、载荷的编码/解码与全部格式校验
  io.rs       Vfs trait；RealVfs（真实 fsync）与 SimVfs（掉电/撕裂注入）
  repo.rs     提交协议、恢复选取、引用链校验、孤儿截断、内存 KV
  demo.rs     确定性崩溃用例、矩阵、三个命名场景
  json.rs     零依赖 JSON 值（请求解析/响应渲染）
  http.rs     标准库 HTTP/1.1 服务与路由
tests/
  persistence.rs       真实文件后端测试
  crash_recovery.rs    崩溃注入验收测试
examples/
  requests.sh / requests.http   HTTP 请求样例
```
