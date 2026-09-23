# 双页超级块恢复（Dual Superblock Recovery）

一个**纯后端**的教学/验证型持久化引擎，用 Rust 标准库实现。它演示了在
“进程/机器可能在任意时刻掉电”的假设下，如何用

- 只追加（append-only）的数据文件，
- 两个**交替覆写**的固定大小超级块页，
- 明确的“先同步数据、再发布根”的提交协议，

做到崩溃后**不丢已提交数据、不暴露未提交数据**，并能在一页超级块损坏时
自动回退到上一完整代次。

无前端、无第三方依赖（`Cargo.toml` 的 `[dependencies]` 为空），断网可构建。

---

## 1. 磁盘格式

一个数据目录包含两个文件：

| 文件 | 大小 | 内容 |
|---|---|---|
| `data.log` | 只增不减 | LEAF（键值）与 ROOT（根快照）记录，均为 `[16B 头][payload]`，头含 magic/类型/长度/CRC32 |
| `super.db` | 固定 8192 B | 两个 4096 B 槽位，代次偶数写槽 0、奇数写槽 1 |

### 数据记录 `data.log`（小端序）

```
偏移  长度  字段
0     4     magic = "DSBR"
4     1     kind：1=LEAF，2=ROOT
5     4     payload_len (u32)
9     4     crc32（对整段记录、本字段按 0 计算）
13    3     保留=0
16    N     payload
```

- LEAF payload：`[u32 klen][key][u32 vlen][value]`
- ROOT payload：`[u64 generation][u32 n]` 后接 n 个
  `[u32 klen][key][u64 leaf_off][u64 leaf_len][u32 leaf_crc]`

数据文件只追加。崩溃可能在文件尾留下半成品记录；挂载时顺序扫描，在**第一条**
CRC/magic/长度不合法的记录处停止，其后的垃圾尾巴被忽略（只追加协议保证
已提交记录之前不会有洞）。

### 超级块页 `super.db`（4096 B/页）

```
偏移     长度  字段
0        4    magic = "DSBS"
4        2    format_version = 1
6        8    generation (u64)
14       8    root_offset (u64)
22       8    root_len (u64)
30       4    root_crc (u32)
34       4    page_crc（对整页 4096B 计算，本字段按 0）
38..4078      保留=0
4078     4    尾戳 magic = "DSBT"
4082     8    尾戳 generation（必须与偏移 6 一致）
4090     4    尾戳 root_crc（必须与偏移 30 一致）
4094     2    保留=0
```

**为什么同时需要整页 CRC 和页尾冗余（尾戳）**：掉电可能让一次 4 KiB 页写
只持久化一部分（torn page）。

- 只校验页头时，“头部已落盘、页尾缺失”的半页会被误判为合法；
- 把 CRC 覆盖整页后，大多数半页（缺失区域是旧页非零残留）会被拒绝；
- 仍有一种信息论上不可检测的情形：缺失的恰好是末尾保留区、且介质返回的
  残留恰好全为零，此时半新页与完整新页逐字节相同。为把有效信息**跨越整页**，
  在页尾再放 generation/root_crc 的冗余副本。只有恰好完整的 4096 B 写入
  才能同时满足整页 CRC 与首尾一致。

> 真实系统里通常还会借助块设备的“原子扇区写”（512/4096 B）或
> checksum+journal；本项目用整页 CRC + 尾戳在纯应用层把检测做到足够强。

---

## 2. 同步边界（提交协议）

一次写入（可含多个键）严格按序执行，**每两步之间都是故障点**：

```
① 追加全部 LEAF 记录        ── fsync(data.log)──   同步点 A
② 追加 ROOT 快照记录        ── fsync(data.log)──   同步点 B（先数据后根）
③ 在交替槽位整页写新超级块   ── fsync(super.db)──  同步点 C（发布点）
```

- ③ 完成前崩溃：新代次从未发布，恢复严格停留在上一代次（可能有已落盘但
  无超级块指向的 LEAF/ROOT，成为不影响正确性的垃圾）。
- 内存键值镜像只在 ③ 成功后更新。
- 超级块永远先写另一个槽位，因此发布过程绝不覆盖唯一的旧有效页。

## 3. 恢复规则（选取最高的完整有效代次）

挂载时：

1. 解码两个槽位，过滤 magic/版本/整页 CRC/尾戳不合法者；
2. 候选按**代次从高到低**排序；
3. 对每个候选，在数据文件上验证：
   - 根指针 `[offset, offset+len)` **必须在文件长度内**（越界直接拒绝，绝不解引用）；
   - 根记录 CRC 正确、确为 ROOT、其代次与超级块一致；
   - ROOT 引用的每个 LEAF 指针在界内、CRC 正确、键一致；
4. 第一个全部通过的候选即恢复结果；全部失败才报错 `CorruptStore`
   （绝不静默使用损坏指针，也绝不凭空回退到某个未经验证的代次）。

---

## 4. 可注入的 I/O 层

引擎只依赖 `Storage` trait（`src/io_layer.rs`），有两个后端：

- `RealStorage`：真实文件（`O_APPEND` + `fsync`）；
- `SimStorage`：进程内模拟磁盘，显式区分**易失页缓存**与**稳定存储**。

故障用 `FaultRule { target, op, nth, kind }` 精确定位“对哪个文件的第几次
写/fsync”注入：

- `FaultKind::CrashBefore`：操作前断电，丢弃全部未同步缓存；
- `FaultKind::TearWrite(keep)`：本次写只有前 `keep` 字节落盘，随后断电
  （半页写）。

重新 `disk.storage(..)` 即一次重新挂载。

---

## 5. 构建与运行

```bash
cargo build --release

# 命令行
./target/release/dual-superblock format  --dir ./storedata
./target/release/dual-superblock serve   --addr 127.0.0.1:8080 --dir ./storedata
./target/release/dual-superblock selftest          # 19 项崩溃恢复自检
```

启动 HTTP 服务后（目录不存在会自动格式化）：

| 方法 路径 | 说明 |
|---|---|
| `GET  /health` | 存活检查 |
| `POST /format` | 重新格式化（截断重建） |
| `GET  /kv` | 列出全部键值（JSON） |
| `PUT  /kv` | 批量写，body：`{"items":[{"key":"a","value":"1"}]}` |
| `GET  /kv/<key>` | 读单键（默认原始字节；`?format=json` 返回 JSON） |
| `PUT  /kv/<key>` | 写单键（body 即值；`-H 'X-Value-Base64: 1'` 可传二进制） |
| `DELETE /kv/<key>` | 删除单键 |
| `GET  /snapshot` | 当前代次/根指针/条目数 |
| `GET  /admin/selftest` | 在模拟磁盘上跑全部崩溃恢复自检（200=全过，500=有失败） |

完整可复制的请求见 [`examples/requests.http`](examples/requests.http)
与 [`examples/demo.sh`](examples/demo.sh)。

---

## 6. 测试

```bash
cargo test               # 全部 27 项
cargo test --test recovery    # 模拟磁盘：崩溃点穷举 / 半页扫描 / 删除 / 覆盖
cargo test --test real_files  # 真实 fsync + 进程外损坏文件
cargo clippy --all-targets    # 零警告
```

- `tests/recovery.rs`：对 `代次0..4 × 6 个故障点` 穷举断电；对超级块半页写
  `keep ∈ {0,1,8,33,37,38,100,2048,4077,4078,4090,4095}` 扫描；多键批次、
  ROOT/LEAF 撕裂、删除、覆盖后旧垃圾不可达等。
- `tests/real_files.rs`：真实文件上的干净往返、毁一个槽位回退、两槽皆坏报错、
  截断数据尾 + 毁超级块、伪造越界根指针、未格式化目录。
- `src/selftest.rs`：19 个具名验收场景（同时供 CLI 与 HTTP `/admin/selftest`）。

---

## 7. 代码地图

```
src/
  crc.rs         CRC-32/IEEE（标准校验向量 0xCBF43926）
  format.rs      磁盘格式：记录/ROOT/超级块页 编解码、指针边界、扫描、槽位
  io_layer.rs    Storage trait、RealStorage、SimDisk/SimStorage（故障注入）
  store.rs       提交协议、恢复候选选取与引用校验
  selftest.rs    19 项崩溃恢复验收场景
  http_server.rs 纯 std 的最小 HTTP/1.1 验证入口
  main.rs        serve / format / selftest 子命令
tests/
  recovery.rs    模拟磁盘崩溃矩阵
  real_files.rs  真实文件损坏恢复
```

## 8. 范围与非目标

- 单线程、本地验证用途；不是生产数据库（无并发控制、无压缩/GC、无 WAL 复用）。
- 只承诺 `fsync` 提供的持久性语义；底层若有带电池的写缓存，行为只会更强。
- 键/值按不透明字节处理；HTTP 层批量接口用固定形状 JSON（非通用 JSON 解析器）。
