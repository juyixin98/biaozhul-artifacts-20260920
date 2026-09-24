# ext-hash-index — 磁盘可扩展哈希页索引服务（Rust + Axum）

纯后端 KV 服务：单文件、固定大小页（4096 B）、固定桶容量的**可扩展哈希（Extendible Hashing）**
页索引。支持目录倍增、桶分裂、删除后的安全合并与目录收缩；哈希函数可注入（含恒值哈希，
用于确定性复现全碰撞）；所有结构性变更带崩溃意图（intent）记录，进程在任意 fsync 边界
"断电"后重开可自动恢复到一致状态。

- 语言/框架：Rust 2021，[Axum](https://github.com/tokio-rs/axum) 0.8（HTTP），Tokio，Serde
- 存储：一个普通文件，由 4096 字节页组成（头页 / 目录页 / 桶页），无外部数据库
- 无 unsafe、无 mmap、无 C 依赖

---

## 1. 依赖与环境

- Rust / Cargo（在 `rustc 1.98.1`、`cargo 1.98.1` 上实际验证；edition 2021）
- 仅需能 `cargo build`（会拉取 axum / tokio / serde 等，见 `Cargo.lock`，版本已锁定）
- Linux/macOS（开发与测试在 Linux x86_64 上完成）

构建：

```bash
cargo build --release
# 产物：target/release/ext-hash-index      （HTTP 服务）
#       target/release/crash-runner        （崩溃注入测试工具）
```

---

## 2. 启动命令

```bash
# 新建索引（文件不存在则按给定参数创建）
./target/release/ext-hash-index \
    --file ./data/index.db \
    --addr 127.0.0.1:3000 \
    --capacity 8 \
    --max-depth 20 \
    --hash fnv1a64

# 文件已存在时即为打开；--capacity/--hash 等创建参数被忽略，
# 哈希函数与容量限制记录在文件头中，重开始终沿用。
./target/release/ext-hash-index --file ./data/index.db
```

参数：

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `--file` | `index.db` | 索引文件路径（不存在则创建） |
| `--addr` | `127.0.0.1:3000` | HTTP 监听地址 |
| `--capacity` | `8` | 每个桶固定容量（键值对条数） |
| `--max-depth` | `20` | 目录最大全局深度，范围 1..=30（`2^max_depth` 槽） |
| `--key-max` | `128` | 单键最大字节数 |
| `--val-max` | `256` | 单值最大字节数 |
| `--hash` | `fnv1a64` | 哈希函数：`fnv1a64` / `constant` / `u64lowbits` |

哈希函数：

- `fnv1a64`：64 位 FNV-1a，分布良好，生产默认。
- `constant`：对任何键都返回 0。**用于复现全碰撞**，验证服务返回明确的容量错误。
- `u64lowbits`：把键解析成十进制 u64 并以其本身作为哈希，低比特即键值，
  测试可精确构造分裂/合并场景。

若上次运行在结构变更中途崩溃，启动时会自动重放 intent，并打印：

```
[ext-hash-index] WARNING: recovered interrupted split operation on open
```

---

## 3. HTTP 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/health` | 健康检查 |
| `GET` | `/stats` | 目录深度、桶分布、槽引用数、空闲页等结构信息 |
| `PUT` | `/keys/{key}` | 插入或覆盖；body `{"value": ...}` |
| `GET` | `/keys/{key}` | 读取 |
| `DELETE` | `/keys/{key}` | 删除（可能触发合并 + 目录收缩） |

值编码：JSON 字符串按 UTF-8 存储；也支持 `{"hex":".."}`、`{"base64":".."}` 原始字节。
读取时 UTF-8 返回字符串，否则返回 `{"hex":".."}`。

状态码：`200` 正常；`413` 键/值超长；`507` 容量耗尽（全碰撞或达到最大深度）；
`400` 值编码错误。

---

## 4. 请求样例（curl）

```bash
# 健康检查
curl -s localhost:3000/health
# {"status":"ok"}

# 插入
curl -s -X PUT localhost:3000/keys/alice \
    -H 'content-type: application/json' \
    -d '{"value":"{\"phone\":\"123\"}"}'
# {"inserted":true,"key":"alice"}

# 覆盖（inserted=false）
curl -s -X PUT localhost:3000/keys/alice -H 'content-type: application/json' \
    -d '{"value":"v2"}'

# 读取
curl -s localhost:3000/keys/alice
# {"key":"alice","found":true,"value":"v2"}

# 原始字节值（hex/base64）
curl -s -X PUT localhost:3000/keys/bin -H 'content-type: application/json' \
    -d '{"value":{"hex":"00ff"}}'

# 删除
curl -s -X DELETE localhost:3000/keys/alice
# {"key":"alice","deleted":true}

# 结构视图
curl -s localhost:3000/stats
```

`/stats` 示例（capacity=4、低比特哈希插入 0..4 后，目录倍增一次、根桶分裂为两个）：

```json
{
  "hash": "u64lowbits",
  "global_depth": 1,
  "directory_slots": 2,
  "bucket_count": 2,
  "bucket_capacity": 4,
  "recovered_intent": null,
  "buckets": [
    {"page": 3, "local_depth": 1, "entries": 3, "slot_refs": 1, "keys": ["0","2","4"]},
    {"page": 4, "local_depth": 1, "entries": 2, "slot_refs": 1, "keys": ["1","3"]}
  ]
}
```

`slot_refs` 恒等于 `2^(global_depth - local_depth)`：它直接体现**多个目录槽共享同一
桶引用**这一可扩展哈希核心不变量。

### 全碰撞 → 明确容量错误（验收项）

```bash
./target/release/ext-hash-index --file /tmp/coll.db --capacity 2 --hash constant
# 另一个终端：
curl -s -X PUT localhost:3000/keys/a -d '{"value":"1"}' ...
curl -s -X PUT localhost:3000/keys/b -d '{"value":"2"}' ...
curl -s -X PUT localhost:3000/keys/c -d '{"value":"3"}' -w '\nHTTP %{http_code}\n'
```

```json
{"error":"capacity exhausted: bucket full (2 entries) and key \"c\" collides with all
 stored keys at hash 0x0000000000000000; no split can separate them",
 "code":"hash_collision_capacity"}
HTTP 507
```

引擎在分裂前先判断"新键哈希是否与桶内全部键完全相同"：若完全相同，任何深度的分裂都
无法把它们分开，立即返回 `CollisionCapacity`（而不是无限倍增目录）。达到 `--max-depth`
但键仍可区分时返回 `DepthLimit`。

---

## 5. 磁盘格式与崩溃恢复（设计要点）

单文件，4096 B 固定页：

- 页 0：头页（魔数、版本、哈希 id、容量、全局深度、桶数、空闲链表头、高水位、目录起点、
  **intent 记录**、FNV 校验和）。
- 目录页：连续的 `u32` 桶页号数组，每页带校验和。
- 桶页：局部深度 + 若干 `(klen,vlen,key,value)`，带校验和。

结构性变更（分裂 / 合并 / 收缩）采用 **shadow copy + 两阶段提交 + intent 重放**：

1. **保留页**：桶页从空闲链表取 / 高水位 bump，目录运行时（run）只 bump 分配；先落一个
   "保留头页"使分配结果持久。
2. **写 intent**：头页记录操作类型与全部涉及的页号；此时旧目录、旧桶都还活着。
3. **构建新页**：结果桶写入全新页，新目录写入全新的连续 run；旧页一个字节都不覆盖。
4. **提交阶段 1**：头页原子切换到新目录/新深度/新桶数（intent 仍在），fsync。
5. **提交阶段 2**：把不再可达的旧**桶页**挂到空闲链表并清掉 intent，fsync。

打开文件时若发现 intent 未清，则无条件重放：识别崩溃处于阶段 1 之前还是之后，把自身
状态规范化为"移动前"，只从未被改动的旧页重建新页（绝不信任可能写了一半的新页），再写
最终提交头页。因此在**任意 fsync 边界**崩溃都能恢复为一致状态。

合并的槽位映射严格按低比特可扩展哈希规则：深度 `ld` 的桶拥有步长为 `2^ld` 的等间距槽位
序列，等深 buddy 翻转第 `ld-1` 位；只有两个**不同页且等深**且合并后条数不超过容量的桶
才允许合并，避免把共享浅桶的别名错误改向。目录页不回收（只 bump），从根本上杜绝"旧目录
槽仍引用某页、而该页被复用成桶"的悬挂引用；目录总占用上界为 `2^max_depth` 槽。

---

## 6. 崩溃注入工具与自动化测试

`crash-runner` 是测试辅助二进制：打开索引、安装在指定持久化点硬退出（`_exit(9)`，
模拟断电）的钩子，然后执行一条 put/delete。

```bash
cargo run --bin crash-runner -- --file ./t.db --op put --key 4 --value v \
    --capacity 4 --hash u64lowbits --crash split.after_directory
# 退出码 9 = 模拟崩溃；之后用服务或 Index::open 重开即触发重放
```

可注入点：`split.after_reservation | split.after_intent | split.after_buckets |
split.after_directory | split.after_commit_meta`，`merge.*` 对应五点（含
`after_reservation`），`shrink.after_intent`。

- 崩在 `after_reservation`：保留头页已落盘、intent 尚未写。打开时无 intent，操作视为
  未发生，仅泄漏少量不可达保留页，状态完全一致（有专门测试）。
- 崩在 `after_intent/after_buckets/after_directory/after_commit_meta`：打开时重放 intent，
  收敛到操作完成的一致状态。

运行全部测试（单元 + 集成）：

```bash
cargo test
```

测试组成：

- `tests/smoke.rs`：分裂/合并/收缩往返、持久化重开、全碰撞错误、覆盖语义。
- `tests/crash_recovery.rs`（8 个）：在分裂（倍增分裂与**共享桶非倍增分裂**）、合并
  （不收缩的合并与合并后收缩）、收缩的**每个崩溃点**制造断电，并额外覆盖"保留头页后、
  intent 前"窗口；重开后验证：
  - 打开成功且 `recovered_intent` 正确；
  - 崩溃前数据完好、崩溃中那条写要么全有要么全无；
  - `slot_refs == 2^(gd-ld)` 等结构不变量成立，目录共享桶引用正确；
  - 恢复后的文件再次打开无残留 intent；
- `tests/model.rs`：确定性 LCG 随机负载（多组容量 × 哈希 × 数千次增删），每一步都与
  内存 `BTreeMap` **全量扫描比对**，并校验目录槽引用计数、局部深度、桶容量等不变量；
  重开后再次全量比对。容量 1 的用例强制每次插入都倍增、每次删除都合并。
- `tests/http_api.rs`：真实 TCP 起 Axum 服务，手写 HTTP/1.1 请求验证 CRUD、`/stats`、
  全碰撞 507、超长 413，以及"先崩溃再用 HTTP 服务打开自动恢复"。
- 库内单元测试：哈希函数、base64/hex 解码。

---

## 7. 实际运行结果（如实记录）

在本机（Linux x86_64，rustc 1.98.1）实际执行：

- `cargo build --release`：成功，无警告。
- `cargo test`：**全部通过**（24 个测试：4 个库单元 + smoke 3 + crash_recovery 8 +
  model 5 + http_api 4）。模型测试单组最长约 12 秒（数千次操作 + 每步全量比对）。
- 真实启动服务并用 curl 验证：
  - capacity=4、u64lowbits 插入 0..4：`global_depth` 0→1，桶 1→2，键按哈希位正确分布；
  - 依次删除后合并级联、目录收缩回 `global_depth=0`、桶数回到 1；
  - constant 哈希、capacity=2：第 3 个不同键返回 `HTTP 507`
    `hash_collision_capacity`，且失败插入不可见；
  - `crash-runner` 在 `split.after_directory` 退出码 9，服务重开打印
    "recovered interrupted split"，旧键可读、未提交的键 4 不存在且随后可正常插入。

### 已知限制 / 未完成项

- **非并发写**：单实例内用 `Mutex` 串行化所有操作；不支持多进程同时打开同一文件
  （无文件锁）。需要多实例时应在前面加一层单写者协调。
- **目录页不回收**：分裂/合并产生的旧目录运行时只 bump、不复用，文件随历史结构变更次数
  缓慢增长（桶页仍通过空闲链表复用）。上界约为每次结构变更一页；未做在线压实（compaction）。
- **耐久性依赖文件系统语义**：持久性通过 `fdatasync` 实现；在不尊重刷新顺序的异常设备上
  （例如部分写回缓存且无电池保护的硬件），页内校验和能检出撕裂写入，但不能替代真正的
  fsync 保证。
- 键值均为原始字节（HTTP 层提供字符串/hex/base64 编码），未实现范围扫描、TTL、事务等。
- 仅做了纯后端 HTTP，无任何前端界面（按要求）。
