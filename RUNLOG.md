# 运行记录（RUNLOG）

环境：Linux 6.8、`rustc 1.98.1`、`cargo 1.98.1`。日期 2026-09-23。
本文件记录实际执行的命令与结果；所有“通过/拒绝”结论均来自真实运行输出。

## 1. 编译

```
$ cargo build --release
    Finished `release` profile [optimized] target(s)
$ cargo build --release 2>&1 | grep -c warning
0
$ cargo fmt --check
（无差异）
```

## 2. 自动化测试

```
$ cargo test
running 29 tests
test result: ok. 29 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out
```

测试覆盖（29 个 = hex_codec 1 + merkle 12 + vfs 4 + store 10 + http 2）：

- `hex_codec`：hex 往返与非法输入（1）
- `merkle`（12）：分块计数/切分；空根稳定性与 chunk_size 绑定；叶/内部域分离与 level 绑定；
  单块树；奇数提升层宽形状；**增量更新 vs 全量重建根一致（n=1..=33）**；
  **穷举 n=1..=30 全部连续区间的证明生成+验证（数千个区间，含全部奇数提升形状）**；
  首/尾/全范围（n=1..=17）；空文件证明规则；伪造长度三类攻击；
  错位/篡改/截断步骤/伪造根；JSON 往返
- `vfs`（4）：内存 FS 读写、崩溃截断未 sync 尾部/删除未 sync 文件、写错误钩子、真实 FS 往返
- `store`（10）：空仓库空根；写入/增长/缩短/持久化重开；多块更新脏路径；
  首/尾/范围证明且证明体积不含全文件；干净提交后 journal 为空；
  **10 个故障点逐个注入 I/O 错误后重开自洽**；
  **10 个故障点逐个注入崩溃后状态原子（提交点前=旧状态，提交点后=新状态）**；
  journal 坏帧（截断/校验和/魔数/垃圾尾）识别为 Torn；提交帧前滚；真实磁盘 I/O 错误恢复
- `http`（2）：开仓→写→首块证明→独立验证→篡改/伪造长度拒绝 的端到端；空文件证明返回 400

> 开发期间曾用 n≤40 的外部 example 做 11480 个区间的穷举（0 失败），
> 固化为测试时收敛到 n≤30 以保持秒级运行；example 已删除。

## 3. 真实 HTTP 端到端（`requests/curl-demo.sh` 实际输出）

```
$ ./target/release/imerkle serve --addr 127.0.0.1:8099 --data-dir ./data
imerkle listening on http://127.0.0.1:8099
$ bash requests/curl-demo.sh
```

关键结果（均为实际响应）：

| 步骤 | 操作 | 结果 |
|---|---|---|
| 1–2 | 开仓 / 空文件状态 | `chunk_count=0`，空根 `35ef80ba…316bb6` |
| 3 | 写 50 字节（4 块，尾块 2 字节） | `chunks_written=4`，根 `6bd3201f…e7b7` |
| 5 | 首块 [0,1) 证明独立验证 | `{"valid":true}` |
| 6 | 尾块 [3,4)（奇数边界） | `{"valid":true}` |
| 7 | 全范围 [0,4) | `{"valid":true}` |
| 8 | 仅改最后一块 1 字节 | **`chunks_written=1`**，根变为 `fe9bd940…df4f` |
| 9 | 篡改块内 1 字节 | `valid:false`，root mismatch |
| 10 | 伪造文件长度 50→99 | `valid:false`，total_chunks inconsistent |
| 11 | 错位（首块证明声称尾块） | `valid:false`，missing required sibling |
| 12 | 伪造承诺根为全 0 | `valid:false`，root mismatch |
| 13 | 空文件仓库请求证明 | HTTP 400 |

崩溃恢复人工验证：提交 2 个版本后杀掉服务进程，用同一 `--data-dir` 重启并 POST open，
状态恢复为 `version=2`、根 `fe9bd940…df4f`，尾块证明 `valid:true`。

磁盘布局核对：干净提交后 `data/demo/` 含 `manifest.json`、`journal`（0 字节）、
`chunks/0..3`；`tmp/staging/` 为空。

## 4. 验收点对照

| 要求 | 结果 |
|---|---|
| 与全量重建比较根 | ✅ `incremental_matches_full_rebuild`、`update_few_blocks_only_dirty_paths`、`write_grow_shrink_and_persist` |
| 验证首/尾范围 | ✅ 穷举全部区间（含首尾、奇数提升边界）+ HTTP 实测 |
| 空文件处理 | ✅ 确定空根；空文件非空区间证明被拒（BadRange / HTTP 400） |
| 伪造长度 | ✅ `LengthMismatch`（单元 + HTTP） |
| 错位证明 | ✅ `MissingSibling`/`RootMismatch`（单元 + HTTP） |
| 更新少量块仅重算受影响路径 | ✅ `chunks_written=1` 实测；`update_levels` 只算脏双亲 |
| 验证者不读完整文件 | ✅ 证明只携带区间块，`/verify` 无状态、不接触仓库 |
| 磁盘格式与同步边界明确 | ✅ README 第 2 节 + 10 故障点错误/崩溃矩阵测试 |
| 可注入 I/O 模拟故障 | ✅ `Vfs`/`MemVfs`/`FaultPolicy`，内存与真实磁盘均测 |
| 源码/README/请求样例/自动化测试 | ✅ 见仓库文件 |
| 无前端 | ✅ 仅后端 HTTP JSON |

## 5. 已知限制 / 未做项（如实说明）

- 崩溃语义在**内存 VFS** 上是建模的（按“最后 fsync 长度”截断）；真实磁盘测试只注入了
  普通 I/O 错误，未在真机制造强制掉电（需额外内核/电源手段）。`RealVfs` 使用真实
  `File::sync_all`，在支持原子 rename 的本地文件系统上遵循文档中的同步边界。
- HTTP 为教学级实现（`std::net`、无 TLS/鉴权、单仓库互斥锁串行写），不面向公网部署。
- 同形树走增量；块数变化（增长/缩短）导致树形变时按设计全量重建。
- `chunk_size` 为仓库级参数，不同值的仓库不能混用（打开即校验并报错）。
