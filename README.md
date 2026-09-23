# 页级写时复制（Copy-on-Write）分支快照存储服务

用 Rust + Axum 实现的**纯后端**页级 COW 存储。快照（分支）可以创建、分支、写入、删除、读取；
分支之间共享未修改的物理页；共享页引用计数与快照根指针的更新是**崩溃安全（crash safe）**的；
写一页只复制「该叶子页 + 一个新根页」共 **2 个物理页**，与快照总页数无关——不会每次复制全部数据。

- 语言/框架：Rust（stable, edition 2021）+ Axum 0.7 + Tokio + serde
- 存储：普通本地文件系统（只依赖 POSIX `rename` / `fsync`，无外部数据库）
- 页面：每页最多 4096 字节，每个快照固定 64 个槽位（256 KiB 可寻址空间，见“设计说明”）

## 目录

- [快速开始](#快速开始)
- [HTTP 接口与请求样例](#http-接口与请求样例)
- [崩溃注入与恢复](#崩溃注入与恢复)
- [运行测试](#运行测试)
- [设计说明：为什么是崩溃安全且省复制的](#设计说明为什么是崩溃安全且省复制的)
- [验收场景与结果](#验收场景与结果)
- [未完成项 / 限制](#未完成项--限制)

## 快速开始

依赖：Rust stable（≥1.74）与 C 工具链（`cc`）。无需数据库、无需网络以外的系统服务。

```bash
cargo build --release

# 启动（默认监听 0.0.0.0:8080，数据目录 ./cow-data）
COW_DATA_DIR=./cow-data COW_ADDR=127.0.0.1:8080 ./target/release/cow-server
```

环境变量：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `COW_DATA_DIR` | `cow-data` | 存储目录，不存在会自动创建 |
| `COW_ADDR` | `0.0.0.0:8080` | 监听地址 |
| `COW_CRASH_KILL` | 未设置 | 设为 `1` 时，注入的故障点真实调用 `exit(37)`（测试/演示用） |

一键演示（起服务 → 两级分支 → 交错覆盖 → 删分支 → 真实进程崩溃与恢复）：

```bash
./scripts/demo.sh
```

## HTTP 接口与请求样例

所有请求/响应均为 JSON。页面字节经标准 base64（带填充）传输；`PUT` 页也支持直接发
`Content-Type: application/octet-stream` 的原始字节。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/` | 接口清单 |
| GET | `/healthz` | 存活检查 |
| GET | `/stats` | 分支、存活页 id 集合、引用计数、累计分配页数（=累计复制页数） |
| GET/POST | `/branches` | 列出 / 创建快照；首个分支不带 `parent` |
| GET/DELETE | `/branches/:name` | 快照元数据 / 删除快照（共享页继续存活） |
| GET/PUT | `/branches/:name/pages/:index` | 读取 / 写入一页（index 0..63） |
| GET/PUT/DELETE | `/fault` | 查看 / 布防 / 解除崩溃注入点 |

写入响应里的 `copied_pages` 即本次操作实际复制的物理页数：**建分支 = 1（仅根页），写页 = 2**。

### curl 样例

```bash
BASE=http://127.0.0.1:8080

# 1) 第一个快照
curl -sS -X POST $BASE/branches -H 'content-type: application/json' \
  -d '{"name":"main"}'

# 2) 写第 0 页（JSON + base64）
printf 'hello-page-0' | base64 | tr -d '\n'     # => aGVsbG8tcGFnZS0w
curl -sS -X PUT $BASE/branches/main/pages/0 \
  -H 'content-type: application/json' \
  -d '{"data_b64":"aGVsbG8tcGFnZS0w"}'

# 2b) 或者直接发原始字节
curl -sS -X PUT $BASE/branches/main/pages/1 \
  -H 'content-type: application/octet-stream' \
  --data-binary 'raw-page-1'

# 3) 读取
curl -sS $BASE/branches/main/pages/0
# {"branch":"main","index":0,"present":true,"page_id":6,"size":12,"data_b64":"aGVsbG8tcGFnZS0w"}

# 空槽位
curl -sS $BASE/branches/main/pages/9
# {"branch":"main","index":9,"present":false,"page_id":0,"size":0}

# 4) 两级分支：main -> child -> grandchild（叶子页零复制，仅复制根页）
curl -sS -X POST $BASE/branches -H 'content-type: application/json' \
  -d '{"name":"child","parent":"main"}'
curl -sS -X POST $BASE/branches -H 'content-type: application/json' \
  -d '{"name":"grandchild","parent":"child"}'

# 5) 在 child 上覆盖第 0 页；main / grandchild 的内容不受影响
curl -sS -X PUT $BASE/branches/child/pages/0 \
  -H 'content-type: application/json' \
  -d '{"data_b64":"'$(printf 'child-new' | base64)'"}'

# 6) 观察存活页集合与引用计数（共享页 rc=2，私有页 rc=1）
curl -sS $BASE/stats | python3 -m json.tool

# 7) 删除 child；它与 grandchild 共享的页不会被回收
curl -sS -X DELETE $BASE/branches/child
curl -sS $BASE/branches/grandchild/pages/1   # 仍可读
```

## 崩溃注入与恢复

布防一个故障点后，下一个走到该点的**写/分支/删除**操作会在该点崩溃。服务进程下一次
`Store::open`（启动时自动执行）会运行恢复：遍历 MANIFEST 中所有已提交根页，重建引用计数、
删除不可达孤儿页与残留临时文件。

### 通过 HTTP（崩溃以 panic 方式中止请求，进程不退出）

```bash
curl -sS -X PUT $BASE/fault -H 'content-type: application/json' \
  -d '{"point":"before_manifest"}'
# 之后一次写操作会在发布根指针之前中断；重放该写即可
curl -sS -X DELETE $BASE/fault
```

### 真实进程崩溃（exit 37）+ 重放

启动服务或 CLI 时设置 `COW_CRASH_KILL=1`，故障点会真实杀死进程（已 `fsync` 的状态才存活）。
仓库附带 `crash-driver` 命令行工具，直接操作同一套磁盘文件，供脚本化崩溃测试：

```bash
cargo build
D=/tmp/cow-demo
./target/debug/crash-driver --dir $D init
./target/debug/crash-driver --dir $D branch main
./target/debug/crash-driver --dir $D write main 0 'page-0'
./target/debug/crash-driver --dir $D branch child main

COW_CRASH_KILL=1 ./target/debug/crash-driver --dir $D fault before_manifest
COW_CRASH_KILL=1 ./target/debug/crash-driver --dir $D write child 0 new
# 退出码 37：在提交点之前被杀

# 重新打开即恢复；child[0] 仍是旧值（操作原子地“全有或全无”）
./target/debug/crash-driver --dir $D read child 0
./target/debug/crash-driver --dir $D fault-clear
./target/debug/crash-driver --dir $D write child 0 new   # 重放成功
```

可注入点（围绕每个磁盘步骤，逐点覆盖）：

- 写页：`before_page/after_page`、`before_setrc/after_setrc`、`before_root/after_root`、
  `before_rootrc/after_rootrc`、`before_manifest/after_manifest`、`before_free/after_free`
- 建分支：`before_root/after_root`、`before_rootrc/after_rootrc`、
  `before_share/after_share`（逐页加共享引用）、`before_manifest/after_manifest`
- 删分支：`before_manifest_del/after_manifest_del`、`before_dec`、`before_free/after_free`

支持 `{"point":"before_share","nth":2}` / CLI `fault-nth <point> <n>`：只在该点第 n 次命中时崩溃。

## 运行测试

```bash
cargo test
```

测试分四组（均为自动化）：

1. `tests/store_test.rs` — 基本读写、持久化重开、分支共享、写时隔离、精确复制计数；
2. `tests/acceptance_test.rs` — **验收场景**：建两级分支（main→child→grandchild），交错覆盖，
   删除中间分支，逐快照核对内容、存活页集合（与独立根遍历交叉验证）、引用计数、磁盘文件，
   并对比 COW 与“整快照复制”的复制页数；最后删光分支确认全部物理页回收；
3. `tests/crash_test.rs` — **逐故障点**用真实子进程 `exit(37)` 崩溃、重开恢复、核对
   “全有或全无”、内容不撕裂、磁盘无孤儿页/无泄漏临时文件、引用计数与根遍历一致，随后解除故障重放成功；
   含三分支共享页覆盖、连撞连恢复收敛两个专项；
4. `tests/http_test.rs` — 经 Axum router 的端到端接口测试（JSON base64 与原始字节两种写法）。

## 设计说明：为什么是崩溃安全且省复制的

### 磁盘布局

```
<COW_DATA_DIR>/
├── MANIFEST            # JSON：分支名 -> (根页id, 父分支, 时间戳)；提交点
├── ALLOC               # 下一个可分配页 id（先持久化，id 永不复用）
├── data/<pid>.page     # 不可变页文件
│                       #   根页 = 64 个小端 u64 槽位；叶子页 = 用户字节
├── refcounts/<pid>.rc  # 引用计数（有多少已提交根页的槽位指向它）
└── CRASH               # 故障注入指令
```

### 写一页的提交协议（恰好复制 2 页）

1. 分配叶子页 id，**先 fsync ALLOC**（崩溃也不复用 id）；
2. 新叶子页：写临时文件 → `fsync` → `rename` → `fsync(目录)`；rc 置 1；
3. 分配新根页 id，构造**新根页**：未改动槽位原样指向旧叶子（共享，不复制数据），
   被改槽位指向新叶子；同样临时文件+fsync+rename+fsync目录；
4. **原子替换 MANIFEST**（临时文件+fsync+rename+fsync目录）——这是唯一提交点；
5. 提交后再删除旧根页、对被替换叶子 rc−1（归零才删数据）。

关键点：新根与旧根对未改动叶子的引用数完全相同，所以根指针交换**不需要改动任何共享页的计数**；
一次写永远是 2 个物理页复制，与快照大小无关。

### 建分支（复制 1 页，叶子数据零复制）

复制一份根页（分支私有），把共享叶子的 rc 各 +1，MANIFEST 原子发布新分支。

### 崩溃安全性论证

- 所有文件发布都是 `临时文件写+fsync → rename → fsync(目录)`，不会出现撕裂的页/清单；
- MANIFEST 是单一提交点：崩溃在第 4 步之前 ⇒ 操作完全未发生；之后 ⇒ 已发生，
  清理未做只会留下孤儿页或偏高的计数；
- 清理（减计数、删页）全部在提交之后，删除顺序为“先删 rc 再删数据 + 双目录 fsync”；
  即使中途死掉，被删分支已不在 MANIFEST，不会悬挂引用；
- 打开时的恢复以 MANIFEST 为准做并集遍历：重算全部计数、补写/修正计数文件、
  回收所有不可达页和临时文件，并把 ALLOC 抬到最高页 id 之上。因此**任意崩溃点之后存储都可重开、
  已提交快照逐字节不变、操作全有或全无**（由 `tests/crash_test.rs` 逐点验证）。

## 验收场景与结果

见 `tests/acceptance_test.rs`（功能与数据核对）和 `tests/crash_test.rs`（逐页/逐点故障注入）。
实际运行结果记录在 [`RESULTS.md`](./RESULTS.md)，包括 `cargo test` 输出与 `scripts/demo.sh` 的实际样例。

## 未完成项 / 限制

- 每个快照为**固定单层 64 槽位**（根页直接放叶子指针），没有实现多级页树；
  扩展为多级 B 树式 COW 可支持更大地址空间，协议不变（当前每步仍只复制沿路径的页）。
- 单进程内用 `Mutex` 串行化写操作；未实现多进程并发挂载同一目录。
- 删除分支的“rc−1 循环”中途崩溃靠重启恢复兜底（逐页崩溃测试覆盖），未在活进程内做崩溃续做。
- 故障注入的“真实 kill”依赖 `COW_CRASH_KILL=1`；HTTP 模式下只以 panic 中止单个请求。
- 页内容按字节存储，没有压缩/校验和；MANIFEST 为 JSON 全量重写（快照数量极大时可改为追加日志）。
