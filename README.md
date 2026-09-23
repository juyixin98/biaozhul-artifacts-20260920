# refcount-store — 内容寻址对象库与引用计数回收（Rust + Axum）

纯后端 HTTP 服务，实现一个**内容寻址（content-addressed）对象库**：

- **数据块（block）**：不透明字节，不引用任何对象。
- **清单（manifest）**：JSON `{"refs": ["<hash>", ...]}`，可引用其他清单和数据块。
- 所有对象以其内容的 **SHA-256 十六进制**为键存放（相同内容自动去重）。
- **发布根（root）**：命名指针。从任意根沿清单引用可达的对象即为存活对象。
- **垃圾回收（GC）**：标记-清除。删除根后，仅被它可达的对象成为垃圾并可被回收；被其它根共享的子图继续保留。

并解决两个关键的并发/生命周期问题：

1. **发布根与 GC 并发**：根的增删与每次 GC 共用同一把排他锁（`gc_lock`）。一次“删除旧根 → 发布新根”要么整体在某轮 GC 快照之前完成，要么阻塞到该轮 GC 结束。GC 绝不会看到“根已删除、新根未发布”的中间态，因此**绝不会回收新根可达的对象**。
2. **未完成上传的独立保留期**：上传会话（upload session）中的对象有独立的保留集合。会话未结束时始终保留；结束（complete/abort）后再保留一个独立的宽限期（`RCS_RETENTION` 秒，默认 3600），期间即使没有任何根引用也不会被回收；过期后才可被回收。

---

## 目录结构

```
Cargo.toml              依赖清单
Cargo.lock              锁定依赖（已提交）
src/store.rs            核心引擎：内容寻址存储、引用闭包、上传保留、并发安全 GC
src/http.rs             Axum 路由与错误映射
src/lib.rs / src/main.rs 库入口 / 服务入口
tests/store_test.rs     核心引擎测试（含并发验收测试）
tests/http_test.rs      真实 TCP 端到端 HTTP 测试
examples/demo.sh        curl 全流程演示脚本
```

## 依赖与运行要求

- Rust（stable，2021 edition；开发用版本见下文“实际运行记录”）。用 `rustup` 安装：
  ```bash
  curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y
  ```
- 一个 C 工具链（链接用，一般系统自带 `cc`/`gcc`）。
- 主要 crate：`axum 0.7`、`tokio 1`、`serde 1`、`serde_json 1`、`sha2 0.10`、`hex 0.4`；
  开发依赖 `reqwest 0.12`、`tempfile 3`。所有版本在 `Cargo.lock` 中锁定。
- 无需外部数据库或服务；对象以普通文件落盘（临时文件 + 原子 rename），根与类型元数据分别持久化到 `roots.json`、`kinds.json`。

## 启动命令

```bash
cargo run --release
# 或先构建再运行二进制
cargo build --release
./target/release/refcount-store
```

环境变量：

| 变量 | 默认 | 含义 |
|------|------|------|
| `RCS_DATA_DIR`  | `./data`          | 落盘目录 |
| `RCS_BIND`      | `127.0.0.1:8080` | 监听地址 |
| `RCS_RETENTION` | `3600`           | 已结束上传的独立保留期（秒） |

启动示例（短保留期，方便观察过期回收）：

```bash
RCS_BIND=127.0.0.1:8080 RCS_DATA_DIR=./data RCS_RETENTION=2 cargo run --release
```

健康检查：

```bash
curl -s http://127.0.0.1:8080/ 
# {"status":"ok","objects":0,"upload_retention_secs":3600}
```

## 构建与测试

```bash
cargo test            # 运行全部自动化测试（含并发验收、HTTP 端到端）
cargo build --release # 构建发布二进制
```

---

## HTTP 接口

对象哈希均为 64 位小写十六进制 SHA-256（由服务端计算并返回）。

| 方法 | 路径 | 说明 | 请求体 |
|------|------|------|--------|
| GET    | `/` | 健康检查 | — |
| POST   | `/blocks` | 存入数据块，返回 `{hash,kind}` | 原始字节（任意二进制） |
| POST   | `/manifests` | 存入清单，返回 `{hash,kind}` | `{"refs":["<hash>", ...]}` |
| GET    | `/objects` | 列出全部对象哈希 | — |
| GET    | `/objects/{hash}` | 读取对象原始字节（响应头 `x-object-kind`） | — |
| GET    | `/objects/{hash}/meta` | 类型元数据 `{hash,kind,size}` | — |
| POST   | `/uploads` | 开启上传会话，返回 `upload_id` | — |
| POST   | `/uploads/{id}/blocks` | 向会话存数据块 | 原始字节 |
| POST   | `/uploads/{id}/manifests` | 向会话存清单 | `{"refs":[...]}` |
| POST   | `/uploads/{id}/complete` | 完成（开始计保留期） | — |
| POST   | `/uploads/{id}/abort` | 中止（保留策略相同） | — |
| GET    | `/roots` | 列出根 `{name: hash}` | — |
| PUT    | `/roots/{name}` | 发布/替换根 | `{"hash":"<hash>"}` |
| DELETE | `/roots/{name}` | 删除根 | — |
| POST   | `/gc` | 执行一轮 GC，返回报告 | — |

`POST /gc` 返回：

```json
{ "reachable": 5, "retained_by_upload": 0, "open_uploads": 0, "deleted": ["<hash>", "..."] }
```

错误状态码：`400`（哈希非法/清单格式错）、`404`（对象/会话缺失、发布悬空根、依赖缺失）、`409`（重复结束上传）、`500`（IO）。

> 说明：清单类型是**显式**的（通过 `/manifests` 写入并在 `kinds.json` 记录）。
> 这样即使某个数据块的字节恰好长得像清单 JSON，也不会被 GC 当作引用边遍历。
> 发布根时会校验目标对象及其整个引用闭包都已存在，因此根永远不会悬空。

---

## 请求样例（端到端）

以下脚本一键演示**全部验收点**（需要服务已在 8080 启动；用 `python3` 解析 JSON）：

```bash
BASE=http://127.0.0.1:8080 ./examples/demo.sh
```

手动逐步示例：

```bash
BASE=http://127.0.0.1:8080
C='curl -fsS'

# 1) 两个被共享的数据块
B1=$($C -X POST $BASE/blocks --data-binary 'shared one' | python3 -c 'import sys,json;print(json.load(sys.stdin)["hash"])')
B2=$($C -X POST $BASE/blocks --data-binary 'shared two' | python3 -c 'import sys,json;print(json.load(sys.stdin)["hash"])')

# 2) 共享清单 shared -> {b1,b2}
SHARED=$($C -X POST $BASE/manifests -H 'content-type: application/json' \
  -d "{\"refs\":[\"$B1\",\"$B2\"]}" | python3 -c 'import sys,json;print(json.load(sys.stdin)["hash"])')

# 3) 两个根清单都引用 shared
MA=$($C -X POST $BASE/manifests -H 'content-type: application/json' -d "{\"refs\":[\"$SHARED\"]}" | python3 -c 'import sys,json;print(json.load(sys.stdin)["hash"])')
MB=$($C -X POST $BASE/manifests -H 'content-type: application/json' -d "{\"refs\":[\"$SHARED\"]}" | python3 -c 'import sys,json;print(json.load(sys.stdin)["hash"])')

# 4) 发布两个根 + 一个无人引用的孤儿块
$C -X PUT  $BASE/roots/A -H 'content-type: application/json' -d "{\"hash\":\"$MA\"}"
$C -X PUT  $BASE/roots/B -H 'content-type: application/json' -d "{\"hash\":\"$MB\"}"
ORPHAN=$($C -X POST $BASE/blocks --data-binary 'nobody refs me' | python3 -c 'import sys,json;print(json.load(sys.stdin)["hash"])')

# 5) 删除根 A 并 GC：共享子图因根 B 仍可达而保留；A 的清单与孤儿被回收
$C -X DELETE $BASE/roots/A
$C -X POST $BASE/gc

# 6) 未完成上传：开会话、放块；无任何根引用，但 GC 保留
UP=$($C -X POST $BASE/uploads | python3 -c 'import sys,json;print(json.load(sys.stdin)["upload_id"])')
UB=$($C -X POST $BASE/uploads/$UP/blocks --data-binary 'half uploaded' | python3 -c 'import sys,json;print(json.load(sys.stdin)["hash"])')
$C -X POST $BASE/gc                 # open_uploads=1，该块仍在
$C -X POST $BASE/uploads/$UP/complete
# ... 等待 RCS_RETENTION 秒后再 GC，该块才会出现在 deleted 中
```

### 并发发布 + GC 重启（验收核心）

该场景由自动化测试 `concurrent_publish_vs_restarting_gc_never_drops_live_objects`
确定性覆盖：一个线程在紧密循环里反复重启 GC，多个发布者线程各自反复
“删除自己的根 → 用全新子图重新发布”，发布后立即断言新闭包四块对象齐全。
若 GC 能观察到删除-发布的时间窗，就会误删可达块导致断言失败；测试通过即证明安全。
最后一轮 GC 后，只有各发布者最后一版闭包存活，中间被取代的子图全部被回收。

手工制造并发也可以——要点是**先在一个打开的上传会话里暂存新闭包**（这样在发布前、
没有任何根引用的窗口里，对象由“未完成上传保留”保护），再让 GC 与发布并发：

```bash
UP2=$(curl -fsS -X POST $BASE/uploads | python3 -c 'import sys,json;print(json.load(sys.stdin)["upload_id"])')
C1=$(curl -fsS -X POST $BASE/uploads/$UP2/blocks --data-binary 'new root data' \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["hash"])')
MC=$(curl -fsS -X POST $BASE/uploads/$UP2/manifests -H 'content-type: application/json' \
  -d "{\"refs\":[\"$C1\"]}" | python3 -c 'import sys,json;print(json.load(sys.stdin)["hash"])')
curl -s -X POST $BASE/gc >/tmp/gc1.json &      # 回收（重启）进行中：打开的上传保护在途对象
curl -s -X PUT  $BASE/roots/C -H 'content-type: application/json' -d "{\"hash\":\"$MC\"}"
wait
curl -s -X POST $BASE/uploads/$UP2/complete >/dev/null
curl -s -X POST $BASE/gc                        # 发布后由根可达保留；新对象依旧存在
```

> 为什么不能“先裸 PUT 对象、再发布”？对象在发布前若既不被任何根可达、也不属于打开的
> 上传，按定义就是垃圾，可能被并发 GC 合法回收。**未完成上传保留**正是为“在途数据”
> 提供的独立存活理由，这也是该机制存在的意义。

---

## 设计要点（为什么并发是安全的）

- **单一排他 `gc_lock`（tokio::RwLock 写锁）**：`publish_root`、`delete_root`、`gc`
  在修改根集合/遍历根集合期间都持有它。因此根集合在“校验闭包 → 写入根 → 持久化”
  与 GC 的“快照根 → 标记”之间是互斥的。发布的目标闭包在持锁期间被完整校验存在，
  不会出现“先标记为垃圾、后被新根引用”的回收。
- **对象索引写锁冻结写入**：一轮 GC 期间持有对象集合的写锁，使 `put` 无法并发增删，
  标记-清除基于一个一致快照。
- **原子落盘**：对象与元数据都走临时文件 + `rename`，避免半写对象可见。
- **两条独立的存活理由取并集**：根可达集合 ∪ 上传保留集合。上传记录在 GC 开始时
  先剔除“已结束且超过保留期”的会话；未结束会话无条件保留。
- **引用计数语义**：本例以“从根的可达性（mark-sweep）”实现引用计数的回收效果，
  天然正确处理共享子图（多个根引用同一对象时只在全部失去引用后才回收），
  无需维护易产生并发不一致的逐边计数表。

---

## 实际运行记录

（构建与测试的真实命令、工具链版本、结果，以及未完成项见 `RUN_REPORT.md`。）
