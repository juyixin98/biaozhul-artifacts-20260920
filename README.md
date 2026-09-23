# 磁盘 B+ 树整数键索引服务（Rust + Axum）

纯后端的**磁盘型 B+ 树整数键索引服务**。数据保存在定长页文件中，支持：

- 插入 / 覆盖（`i64` 键 → `u64` 值）
- 删除（自动**借位 / 合并 / 根塌缩**）
- 点查与**有序闭区间范围读**（沿叶子链表顺序扫描）
- 正确处理**根分裂（树高 +1）**、删除后树高重新降回 1、**叶链维护**
- **页大小可配置得很小**（最小 64 字节），以便在少量数据下就触发分裂 / 借位 / 合并边界
- HTTP 接口（[Axum](https://github.com/tokio-rs/axum)），无任何前端

验收用**固定种子随机差分测试**：同一套随机操作同时作用于本 B+ 树与内存 `BTreeMap`，
每一步都校验分隔键、节点占用率、叶链一致性与全量数据；并覆盖树高增加后重新降为一层。

---

## 目录结构

```
.
├── Cargo.toml
├── Cargo.lock                 # 锁定依赖（随源码交付）
├── README.md
├── src
│   ├── lib.rs                 # 库入口，导出各模块
│   ├── main.rs                # bptree-server 启动入口（CLI 参数）
│   ├── pager.rs               # 定长页磁盘/内存存储 + 页分配回收（空闲链表）
│   ├── node.rs                # 叶/内部节点页格式与二进制编解码、容量计算
│   ├── bptree.rs              # B+ 树核心：分裂/借位/合并/叶链 + 全树校验
│   └── http.rs                # Axum 路由与 JSON 接口
└── tests
    ├── differential.rs        # 固定种子差分测试（对照 BTreeMap）+ 持久化重开
    └── http_api.rs            # HTTP 接口端到端测试（真实 TCP）
```

---

## 依赖

- Rust 工具链（stable，Edition 2021；开发时使用 `rustc 1.8x`，最低约 1.74+）
- 第三方 crate（见 `Cargo.toml`，版本锁定在 `Cargo.lock`）：
  - `axum 0.7` — HTTP 服务
  - `tokio 1`（`rt-multi-thread` / `macros` / `net`）— 异步运行时
  - `serde 1`（derive）+ `serde_json 1` — JSON 序列化
- 无其他系统级依赖（不依赖 libclang/数据库等）。

---

## 启动命令

### 1) 构建并运行（开发）

```bash
cargo run --release -- --file ./data/bptree.db --page-size 256 --addr 127.0.0.1:8080
```

参数：

| 参数 | 默认 | 说明 |
|---|---|---|
| `--file`, `-f` | `bptree.db` | 数据文件路径；不存在则新建，存在则打开 |
| `--page-size` | `4096` | **仅新建时生效**；最小 `64`。已存在文件以其文件头记录为准 |
| `--addr`, `-a` | `127.0.0.1:8080` | 监听地址 |
| `--help`, `-h` | — | 帮助 |

> 想立刻触发分裂 / 借位 / 合并边界，用很小的页，例如 `--page-size 64`（叶容量仅 3 项）。

### 2) 仅编译出可执行文件

```bash
cargo build --release
# 产物：./target/release/bptree-server
```

### 3) 运行全部测试

```bash
cargo test --release
```

---

## HTTP 接口与请求样例

所有写请求体均为 JSON；响应也是 JSON。服务监听在 `http://127.0.0.1:8080`。

### 健康检查

```bash
curl -s http://127.0.0.1:8080/healthz
# ok
```

### 插入 / 覆盖 `POST /insert`

```bash
curl -s -X POST http://127.0.0.1:8080/insert \
  -H 'Content-Type: application/json' \
  -d '{"key": 42, "value": 4200}'
# {"ok":true,"inserted":true}
```

- `inserted=true`：新键插入；`inserted=false`：键已存在，值被覆盖。
- 键范围为有符号 64 位整数（`i64`），值为无符号 64 位（`u64`）。

### 点查 `GET /get?key=`

```bash
curl -s 'http://127.0.0.1:8080/get?key=42'
# {"key":42,"found":true,"value":4200}
```

### 删除 `POST /delete`

```bash
curl -s -X POST http://127.0.0.1:8080/delete \
  -H 'Content-Type: application/json' \
  -d '{"key": 42}'
# {"ok":true,"found":true}
```

- `found=false` 表示键本来就不存在，树不发生变化。

### 有序范围读 `GET /range?lo=&hi=`（闭区间，按升序返回）

```bash
curl -s 'http://127.0.0.1:8080/range?lo=-10&hi=10'
# {"lo":-10,"hi":10,"count":N,"items":[{"key":..,"value":..}, ...]}
```

结果沿叶子链表顺序产出，天然全局有序；`lo>hi` 返回 `400`。

### 结构统计 / 自检 `GET /stats`

对整棵树做一次完整一致性校验并返回统计：

```bash
curl -s http://127.0.0.1:8080/stats
```

字段：

```json
{
  "ok": true,
  "page_size": 64,
  "leaf_capacity": 3,
  "height": 3,
  "internal_pages": 5,
  "leaf_pages": 9,
  "item_count": 21,
  "min_occupancy": 0.6666666666666666,
  "max_occupancy": 1.0,
  "occupancies": [ ... 每个节点的占用率（项数或孩子数 / 容量）... ]
}
```

若任一结构不变量被破坏，返回 `500` 与具体错误（哪个页、哪条分隔键 / 占用率 / 叶链出错）。

### 小页边界的快速演示（一页 64 字节，叶容量 3）

```bash
cargo run --release -- -f /tmp/demo.db --page-size 64 &
for k in 1 2 3 4 5 6 7 8; do
  curl -s -X POST localhost:8080/insert -H 'Content-Type: application/json' \
    -d "{\"key\":$k,\"value\":$((k*10))}"; echo
done
curl -s 'localhost:8080/range?lo=3&hi=6'; echo
curl -s localhost:8080/stats; echo
for k in 1 2 3 4 5 6 7 8; do
  curl -s -X POST localhost:8080/delete -H 'Content-Type: application/json' -d "{\"key\":$k}" >/dev/null
done
curl -s localhost:8080/stats; echo   # height 重新变为 1
```

---

## 页格式与算法说明

### 页存储（`src/pager.rs`）

- 文件第 0 页为 64 字节文件头：magic、版本、页大小、根页号、总页数、空闲链表头。
- 其余页为定长 `page_size`。删除回收的页串成**单链空闲表**（页首 8 字节存下一空闲页号），
  分配优先复用回收页。
- 同样的逻辑抽象出 `MemoryPager`，供测试做无磁盘的高速差分。

### 节点格式（`src/node.rs`）

- 叶页：`kind(1) | count(u32) | next(u64)` + `count × (key:i64, value:u64)`；
  键严格升序；`next` 指向右兄弟叶，构成有序叶链。
- 内部页：`kind(1) | child_count(u32)` +
  `child0, sep0, child1, sep1, …, child_{n-1}`。
  路由约定：`child_i` 容纳满足 `sep[i-1] <= key < sep[i]` 的键
  （分隔键是右子树最小键的副本）。
- 容量（`page_size=64` 时）：叶 `(64-13)/16 = 3` 项；内部最多 4 个孩子。
  非根节点最低占用约为容量的一半（向上取整）。

### 分裂 / 借位 / 合并（`src/bptree.rs`）

- **插入**：下降到叶，溢出时叶一分为二（左半复用旧页、右半落新页并**接续叶链 `next`**），
  新分隔键（=右叶首键）上推；父内部页溢出时取中间分隔键上推。**根分裂则新建内部根，树高 +1。**
- **删除**：在叶中删除后若低于最低占用，自底向上修复——
  优先向**左/右兄弟借位（旋转，同步改写父分隔键）**，借不到则与兄弟**合并**
  （叶合并时把 `next` 跨过被吸收页，保证叶链不断）；
  内部根只剩 1 个孩子时**删除旧根、把唯一孩子提为新根，树高 -1**，可一直降回单层叶根。

### 全树校验 `verify()`（每步差分与 `/stats` 都用它）

检查：

1. **分隔键**：每个内部页的分隔键严格升序，且
   左子树所有键 `< sep <=` 右子树所有键（路由正确）；分隔键数 = 孩子数 − 1。
2. **占用率**：所有节点不超过容量上限；非根节点不低于最低占用；根内部页至少 2 个孩子。
3. **叶链**：从最左叶沿 `next` 走出的页序列，必须与中序遍历到的叶序列**完全一致**，
   无环、末端为 `NIL`，且相邻叶的边界键严格递增衔接。
4. 统计树高、内/叶页数、项数与逐节点占用率。

---

## 自动化测试

| 测试 | 位置 | 覆盖内容 |
|---|---|---|
| `differential_against_btreemap_tiny_pages` | `tests/differential.rs` | 页大小 64/80/128/256，各 **12,000 步**固定种子随机增删改查/范围读，逐步对照 `BTreeMap` 并 `verify`；最后清空，断言树高降回 1 |
| `differential_delete_heavy` | 同上 | 72B 极小页、删除密集的 20,000 步负载，重压借位/合并 |
| `file_persistence_across_reopen` | 同上 | 写盘→关闭→重开，数据、页大小、结构一致，且可继续增删 |
| `full_http_workflow` | `tests/http_api.rs` | 真实 TCP 启动服务，覆盖全部接口、覆盖语义、范围读、`/stats`、参数错误、清空后树高归 1 |
| 模块单测 | `src/*.rs` | 分配器复用、节点编解码/容量/路由、增删改查/负数键/树高变化等 |

运行：

```bash
cargo test --release            # 全部
cargo test --release -- --nocapture   # 显示打印
```

---

## 已完成与未完成项（如实记录）

**已完成**

- 磁盘定长页存储 + 空闲页回收复用，文件持久化与重开。
- B+ 树插入/覆盖、根分裂、删除借位（左右旋转）、合并、根塌缩降层、叶链 `next` 维护。
- 点查、有序范围读（叶链扫描）。
- 页大小可配置到 64 字节触发边界。
- Axum HTTP 接口（insert/delete/get/range/stats/healthz）+ JSON 错误处理。
- 固定种子差分测试对照内存有序映射，每步校验分隔键、占用率、叶链；覆盖树高增加后降回 1。
- HTTP 端到端测试、磁盘重开测试；锁定依赖 `Cargo.lock`。

**已知限制 / 未做**

- 仅 `i64 -> u64` 单一固定模式（题目要求整数键索引），未做任意长度键/多列/二级索引。
- 服务端用 `std::sync::Mutex` 串行化写请求；纯后端单机场景够用，未做并发 B+ 树、
  WAL、崩溃恢复（写后 `sync_data` 落盘，但非崩溃原子事务）与压缩。
- 未做范围读的流式分页（当前一次性返回区间内全部结果）。
- 无鉴权 / 多租户 / 前端界面（题目明确仅需纯后端与接口样例）。
