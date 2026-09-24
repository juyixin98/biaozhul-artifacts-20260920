# disk-bptree：磁盘页式 B+ 树整数键索引服务

用 Rust + [Axum](https://github.com/tokio-rs/axum) 实现的**纯后端**磁盘 B+ 树索引服务。
键、值均为 `i64`；支持插入/覆盖、删除、点查与沿叶链的有序范围读。
页大小可配置，最小 **64 字节**（叶仅容纳 3 个键值对），以便在很小的数据量下
频繁触发**根分裂、借位、兄弟合并和逐层坍缩降高**等边界路径。

- 固定页式存储（单文件、0 号页元数据、节点页 + 空闲页链表复用）
- 叶节点双向链表，范围读无需回到根
- 内部节点分隔键满足 `max(左子树) < sep ≤ min(右子树)`
- 删除回溯时先向左右兄弟借位，借不出则合并；内部根只剩 1 个孩子时坍缩降高
- 每次写操作后 fsync（`--no-sync` 可用于压测/测试）
- 内置全量结构校验（分隔键、占用率半满下限、叶链双向一致、所有叶同深）

## 目录结构

```
src/
  pager.rs   固定页磁盘读写、元数据页、空闲链表
  node.rs    叶/内部节点模型、定长页编解码、页大小 → 容量推导
  tree.rs    插入分裂 / 删除借位·合并·根坍缩 / 点查 / 范围读
  verify.rs  全量结构校验与占用率统计
  http.rs    Axum 路由与 JSON 接口
  main.rs    服务入口（命令行参数）
tests/
  differential.rs  固定种子随机操作 vs std::collections::BTreeMap 差分测试
  persistence.rs   持久化、高度升降、页复用、边界键、逆序插入/删除
  http_api.rs      HTTP 端到端测试
examples/requests.sh  curl 请求样例
```

## 依赖

- Rust（开发使用 1.98.1，edition 2021；1.80+ 应可编译）
- 运行时依赖（见 `Cargo.lock`，已锁定）：
  `axum 0.8`、`tokio 1`、`serde 1`、`serde_json 1`
- 构建/测试额外依赖：`rand 0.10`、`tempfile 3`、`tower 0.5`、`http 1`

无系统级依赖（不使用 mmap，仅标准库文件 IO）。

## 启动命令

```bash
# 构建
cargo build --release

# 新建库文件并启动（页大小 64 字节，制造最频繁的分裂/合并）
./target/release/bptree-server \
  --db ./data/demo.db \
  --page-size 64 \
  --addr 127.0.0.1:3000

# 文件已存在时，--page-size 被忽略（以文件头元数据记录的页大小为准）
./target/release/bptree-server --db ./data/demo.db
```

参数（均可用环境变量 `BPTREE_DB` / `BPTREE_PAGE_SIZE` / `BPTREE_ADDR` / `BPTREE_NO_SYNC` 代替）：

| 参数 | 默认 | 说明 |
|---|---|---|
| `--db PATH` | `bptree.db` | 数据库文件路径，不存在则创建 |
| `--page-size N` | `256` | 固定页大小（字节），最小 64；仅建库时生效 |
| `--addr HOST:PORT` | `127.0.0.1:3000` | 监听地址 |
| `--no-sync` | 关 | 跳过逐操作 fsync（崩溃可能丢最近写入，仅用于测试/压测） |

不同页大小下的容量（节点满容量 / 非根半满下限）：

| 页大小 | 叶容量（键值对） | 内部分隔键容量 |
|---|---|---|
| 64–67 | 3（半满 ≥1） | 3（半满 ≥1） |
| 128 | 7（≥3） | 9（≥4） |
| 256 | 15（≥7） | 19（≥9） |

## HTTP 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/healthz` | 健康检查 |
| `GET` | `/keys/{key}` | 点查，返回 `found`/`value` |
| `PUT` | `/keys/{key}` | 插入或覆盖；请求体 `{"value": <i64>}`；新建返回 201，覆盖返回 200 与旧值 |
| `DELETE` | `/keys/{key}` | 删除，返回是否命中 |
| `GET` | `/range?start=&end=&limit=` | 闭区间 `[start,end]` 有序扫描；三个参数均可省略；`start>end` 返回空 |
| `GET` | `/stats` | 页大小、容量、根/最左叶页号、空闲链表头、键数 |
| `POST` | `/verify` | 全量结构校验；通过 200，发现结构破坏 500（正常使用不应出现） |

键为 `i64`（含负数）。错误统一为 `400/422` + `{"error": ...}`。

### 请求样例

```bash
curl -X PUT localhost:3000/keys/42 -H 'content-type: application/json' -d '{"value":100}'
# {"key":42,"value":100,"inserted":true,"replaced":null}

curl localhost:3000/keys/42
# {"key":42,"found":true,"value":100}

curl 'localhost:3000/range?start=10&end=50&limit=20'

curl -X DELETE localhost:3000/keys/42
# {"key":42,"deleted":true}

curl localhost:3000/stats
curl -X POST localhost:3000/verify
```

完整脚本见 [`examples/requests.sh`](examples/requests.sh)。

## 磁盘格式

单文件，固定页：

- **0 号页（元数据，32 字节有效）**：magic、版本、页大小、根页号、最左叶页号、
  空闲链表头、键计数
- **叶页**：`kind(u8) + 保留(u8) + n(u16)`，随后 `n × (i64 key, i64 value)`，
  尾部 `u32 prev, u32 next`（双向叶链）
- **内部页**：页头后 `u32 child0`，随后 `n × (i64 sep, u32 child)`
- 被释放的页用其前 4 字节串成空闲单链表，再次分配时优先复用

键与指针均定长，因此容量由页大小在建库时直接确定；内部容量强制取奇数
（`2t-1`），保证“分裂出的两半”和“两个半满节点 + 一个下推分隔键合并”都恰好
不超出页容量。

## 运行测试

```bash
cargo test                 # 全部 21 个测试（约 40 秒）
cargo test --release       # 更快
cargo test --test differential -- --nocapture   # 查看各种子的最大树高
```

差分测试（`tests/differential.rs`）的验收逻辑：固定种子的伪随机序列
（插入偏置分阶段变化）同时作用于磁盘 B+ 树和内存 `BTreeMap`，**每一步**之后：

1. `verify()` 全量检查：分隔键 `max(左)<sep≤min(右)`、非根节点占用率不低于半满、
   不超过容量、叶链 `next/prev` 双向一致且串联全部叶、所有叶同深；
2. 对键空间内每个键点查，对照模型映射；
3. 多组范围读（全范围、随机区间、单边无界、反向空区间）对照模型；
4. 序列结束后洗牌删除清空整棵树，每删一次重复上述校验，并断言
   树高在小页下确实升到 **≥3 层**后逐步降回**单层**、再到空树，空树后可重新插入。

## 实际运行结果（如实记录）

以下在本机（Linux x86_64，Rust 1.98.1）实际执行的结果。

- `cargo build --release`：无错误无警告通过。
- `cargo test`：**21/21 通过**（lib 7、differential 5、persistence 6、http 3），
  debug 构建约 36 秒。
- 差分测试中 64/67/128/256 字节四种页大小、5 个固定种子全部通过；
  64 字节页下树高在运行中升至 **5 层**（100 键）/ 测试覆盖 ≥3 层，
  清空时逐级坍缩回 0 层后再插入恢复单层。
- HTTP 手工演示（64 字节页，实际 curl 输出）：
  - 顺序插入 12 个键 → 树高 3（6 叶 + 3 内部节点），`verify.ok=true`，
    叶最小占用率 2/3；
  - 逐个删除 1..10 → 第 7 次删除时树高由 3 降为 2；删到空 → 高度 0、
    根/最左叶页号归零、释放页进入空闲链表（`free_list_head=1`）；
  - 关闭进程后用同一文件重启：页大小从文件头恢复，空闲链表持久化；
    重新插入 100 键时回收页全部被复用（链表头归零），文件无空洞增长，
    最终 77 页 = 1 元数据 + 50 叶 + 26 内部节点，树高 5，`verify` 通过。

## 设计取舍与未完成项

- 服务对整棵树使用一把 `std::sync::Mutex` 串行化写/读，未做并发页缓存、
  WAL 或多版本控制；崩溃语义依赖“节点页先落盘、元数据页最后落盘”的顺序，
  未实现崩溃后自动修复（异常断电可能需要从最近一致状态重建）。
- 无缓冲区管理：每次操作直接读页到单个复用缓冲；没有 LRU 缓存，随机点查
  为树高次数的 `pread`。
- 定长行（键、值均为 i64），不支持变长值、重复键或复合键。
- 未做鉴权/TLS；仅监听本机回环为默认值，请勿直接暴露公网。
- 大范围读会一次性把结果收集到内存（HTTP 层还有一次截断），未提供游标分页；
  但底层扫描沿叶链进行，可在此基础上扩展流式响应。
