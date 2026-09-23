# cow-snapstore

页级写时复制（copy-on-write）快照存储服务。纯后端，Rust + Axum，提供 HTTP 接口。
快照可分支（branch）、写入、删除与读取；分支共享物理页，根指针（快照清单）更新崩溃安全，分支不复制任何数据。

## 设计

```
<data-dir>/
  pages/<id>            不可变物理页文件，固定 4096 字节
  snapshots/<name>.json 快照清单（"根指针"）：逻辑页号 -> {物理页 id, 长度}
  meta.json             累计统计（物理页分配次数）
```

- **写时复制**：每次写一页都分配一个全新的物理页，旧页（可能被其他快照共享）绝不被修改。
- **分支零拷贝**：`create_snapshot(from)` 只克隆页表并落盘一份新清单，不复制页数据；
  共享关系不靠引用计数维护，而是由 GC 从所有存活清单实时推导（mark-and-sweep）。
- **崩溃安全协议**（顺序即正确性）：
  1. 新物理页先写临时文件、`fsync`、`rename` 进 `pages/`、`fsync` 目录 —— 页内容先于任何引用持久化；
  2. 清单通过「临时文件 + fsync + 原子 rename + 目录 fsync」提交 —— 根指针切换是原子的；
  3. 崩溃最多留下未被引用的孤儿页，启动时（以及 `POST /gc`）由 GC 回收。
- **故障注入**：环境变量 `COW_CRASH_AT=<注入点>`、`COW_CRASH_AFTER=<n>` 在第 n 次到达
  注入点时 `abort()`（等价 kill -9 / 断电，不运行任何析构）。注入点：
  `page_flushed`、`page_committed`、`commit_tmp_written`、`manifest_committed`、
  `branch_committed`、`delete_committed`。

## 依赖与启动

依赖：Rust 工具链（开发用 1.98.1）；三方 crate 见 `Cargo.toml`，版本由 `Cargo.lock` 锁定。
无系统库依赖（HTTP 客户端测试用 rustls，不需要 OpenSSL）。

```bash
cargo build
COW_DATA_DIR=/tmp/cow-data COW_PORT=3000 ./target/debug/cow-snapstore
# 或 cargo run
```

`COW_DATA_DIR` 默认 `./cow-data`，`COW_PORT` 默认 `3000`，监听 `127.0.0.1`。

## HTTP 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/snapshots` | 创建快照，body `{"name":"b1","from":"main"?}`；带 `from` 即分支 |
| GET | `/snapshots` | 列出全部快照名 |
| GET | `/snapshots/{name}` | 快照信息（逻辑页数、私有/共享物理页数） |
| DELETE | `/snapshots/{name}` | 删除快照（共享页不受影响，孤儿页待 GC） |
| PUT | `/snapshots/{name}/pages/{i}` | 写第 i 页，body 为原始字节（≤ 4096） |
| GET | `/snapshots/{name}/pages/{i}` | 读第 i 页，返回原始字节 |
| GET | `/stats` | 全局统计：页文件数、存活页数、孤儿页数、累计分配页数 |
| POST | `/gc` | mark-and-sweep 回收孤儿页 |

### 请求样例（实际运行输出）

```bash
$ curl -s -X POST localhost:3000/snapshots -H 'content-type: application/json' -d '{"name":"main"}'
{"name":"main","branched_from":null}
$ curl -s -X PUT localhost:3000/snapshots/main/pages/0 -d 'hello' -w '%{http_code}'
204
$ curl -s -X POST localhost:3000/snapshots -H 'content-type: application/json' -d '{"name":"b1","from":"main"}'
{"name":"b1","branched_from":"main"}
$ curl -s -X POST localhost:3000/snapshots -H 'content-type: application/json' -d '{"name":"b2","from":"b1"}'
{"name":"b2","branched_from":"b1"}
$ curl -s localhost:3000/stats        # 分支后：分配页数不变，零拷贝
{"snapshots":3,"page_files":1,"live_pages":1,"orphan_pages":0,"pages_allocated":1}
$ curl -s -X PUT localhost:3000/snapshots/b2/pages/0 -d 'b2-edit' -w '%{http_code}'
204
$ curl -s localhost:3000/snapshots/main/pages/0
hello
$ curl -s localhost:3000/snapshots/b2/pages/0
b2-edit
$ curl -s -X DELETE localhost:3000/snapshots/b1 -w '%{http_code}'
204
$ curl -s -X POST localhost:3000/gc
{"removed_pages":0}
$ curl -s localhost:3000/snapshots/b2
{"name":"b2","logical_pages":1,"present_pages":1,"private_pages":1,"shared_pages":0}
```

完整演示脚本（两级分支 + 交错覆盖 + 删分支 + GC）：

```bash
./scripts/demo.sh
```

## 测试

```bash
cargo test
```

- `tests/engine.rs` —— 验收场景：两级分支、三个快照交错覆盖写、删除中间分支，
  逐页核对各快照内容、存活页集合（13 页）、孤儿页（4 页）与累计复制页数（17 次分配），
  并验证重启后状态一致；另有错误与边界用例（空洞页、超页大小、非法名等）。
- `tests/crash.rs` —— 逐页故障注入：子进程执行确定性脚本，在 5 个提交边界注入点
  分别于第 1..6 次命中时 `abort()`（共注入 27 次真实进程崩溃）；每次崩溃后重开存储，
  校验恢复成功、每个快照内容都是脚本的一致前缀、无悬空页引用、GC 后无孤儿页；
  随后在同目录无注入续跑，状态收敛到完整结果。
- `tests/http.rs` —— 启动真实服务器二进制，通过 HTTP 跑完整验收场景并核对统计数字。

### 实际运行结果（2026-09-23，cargo 1.98.1，Linux x86_64）

```
test result: ok. 2 passed; 0 failed   (tests/crash.rs，含 27 次注入崩溃)
test result: ok. 2 passed; 0 failed   (tests/engine.rs)
test result: ok. 1 passed; 0 failed   (tests/http.rs)
```

手动崩溃演示（服务器以 `COW_CRASH_AT=page_committed` 启动，写第二页时进程 SIGABRT）：

```
put main/1: connection dropped (process aborted)   # 页文件已落盘、清单未提交
[failpoint] aborting at page_committed (hit #1)
# 重启后：
main/0: before-crash                                # 已提交的写入完好
main/1: {"error":"page 1 not present in snapshot main"}  # 未提交的写入原子地丢失
{"snapshots":1,"page_files":1,"live_pages":1,"orphan_pages":0,...}  # 孤儿页已回收
```

## 已知限制 / 未完成项

- `meta.json` 的累计分配计数不与清单提交同事务：崩溃后计数可能少记一次分配
  （仅影响统计数字，不影响数据一致性；存活页集合始终精确）。
- 单把全局互斥锁串行化所有写操作；无并发优化。
- GC 为 stop-the-world 的 mark-and-sweep，仅在启动时和 `POST /gc` 时运行。
- 无认证/鉴权，仅监听回环地址；快照名限制为 `[A-Za-z0-9._-]`。
- 页不做去重/压缩（相同内容的两页仍占两份空间）；共享仅来自分支。
- 清单为整份 JSON，快照页表极大时提交开销随页数线性增长（未做增量清单）。
