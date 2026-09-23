# lsm-server：有序段合并与墓碑（简化 LSM 服务）

纯后端 Rust 服务，演示日志结构合并树（Log-Structured Merge-Tree）的核心机制：

- **内存表（memtable）**：有序 `BTreeMap`，每条写入带全局单调递增版本号 `seq`；
- **不可变有序段（segment）**：memtable 整体刷盘为只读、按键严格有序的 JSONL 文件，按层（level 0/1/2…）组织；
- **点查 / 范围扫描**：按「新 → 老」顺序查 memtable → L0 → L1 → …，同键取 `seq` 最大的版本；范围扫描跨层归并，**每键只输出一次**、墓碑不输出；
- **合并（compaction）**：把 level n 与 level n+1 的全部段归并为 level n+1 的一个新段，同键按版本取最新；
- **墓碑安全规则**：合并时某键的胜出版本是墓碑（删除标记）时，**只有确认更老层（level > n+1）不存在同键才丢弃**；更老层还有旧值时墓碑必须保留，防止旧值复活；
- **原子发布**：新段文件先写 `.tmp` → fsync → rename → fsync 目录；然后清单 `manifest.json` 同样以 tmp + rename **原子切换**。段文件落盘后、清单切换前崩溃只会留下孤儿文件，重启自动清理，已发布的可视图不受影响。

## 依赖

- Rust 工具链（stable，开发使用 1.98.1），用 rustup 安装：`curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh`
- 运行时 crate：`axum 0.8`、`tokio 1`、`serde 1`、`serde_json 1`（版本锁定见 `Cargo.lock`）
- 仅 Linux/macOS：崩溃恢复依赖 POSIX 的 `rename(2)` 原子语义与文件 fsync
- 无外部数据库、无网络存储，数据全部在本地目录

## 启动

```bash
cargo run --release -- --addr 127.0.0.1:3000 --data-dir ./data --max-mem 1000
```

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `--addr` | `127.0.0.1:3000` | 监听地址 |
| `--data-dir` | `./data` | 段文件与 manifest 的目录，不存在会创建；重启即从此恢复 |
| `--max-mem` | `1000` | memtable 键数阈值，达到后写入会自动 flush 成 L0 段 |

健康检查：`curl http://127.0.0.1:3000/` 返回接口清单。

## HTTP 接口与请求样例

### 写入 / 读取 / 删除

```bash
# 写入（body 为 JSON：{"value": "..."}），返回分配到的版本号 seq
curl -sS -X PUT http://127.0.0.1:3000/kv/alice \
  -H 'Content-Type: application/json' \
  -d '{"value":"alice-v1"}'
# {"ok":true,"seq":0}

# 点查（404 表示键不存在或已被墓碑遮盖）
curl -sS http://127.0.0.1:3000/kv/alice
# {"key":"alice","value":"alice-v1","seq":0,"source":"memtable"}

# 删除 = 写入墓碑
curl -sS -X DELETE http://127.0.0.1:3000/kv/alice
# {"ok":true,"seq":3,"tombstone":true}
curl -sS -i http://127.0.0.1:3000/kv/alice   # HTTP/1.1 404
```

`source` 字段标出值来自 memtable 还是某个段（如 `segment:7:L2`），便于观察数据所处的层。

### 范围扫描

```bash
# start 含、end 不含，均可选；limit 可选；结果按 key 升序，同键绝不重复
curl -sS 'http://127.0.0.1:3000/range?start=a&end=m&limit=10'
# {"start_inclusive":"a","end_exclusive":"m","count":2,"items":[{"key":"alice",...},...]}

curl -sS 'http://127.0.0.1:3000/range'   # 全量扫描
```

### 管理接口

```bash
# 手动把 memtable 刷成一个新的 L0 不可变段
curl -sS -X POST http://127.0.0.1:3000/admin/flush
# {"ok":true,"new_segment_id":4}

# 合并 level n 与 n+1（默认 n=0）
curl -sS -X POST 'http://127.0.0.1:3000/admin/compact?level=0'
# {"level":0,"input_segments":[3,4],"output_segment":5,"output_entries":2,
#  "tombstones_kept":1,"tombstones_dropped":0}

# 查看内存表与段清单（层分布、键范围、条目数）
curl -sS http://127.0.0.1:3000/admin/state
```

故障注入（仅用于验收，会在**新段已 fsync、manifest 切换前**杀死当前进程）：

```bash
# 进程立即以退出码 42 退出，模拟掉电；重启后孤儿段被清理、清单保持旧版本
curl -sS -X POST 'http://127.0.0.1:3000/admin/compact?level=0&fault=exit_after_write'
# panic_after_write：同位置在进程内 panic（不杀进程），用于库级测试
```

## 磁盘布局

```
data/
├── manifest.json        # 段清单（原子切换的唯一真相来源）
├── seg-000001-L0.jsonl  # 第 1 行是段头，其余每行一条 {k,s,v}；v=null 即墓碑
├── seg-000002-L1.jsonl
└── *.jsonl.tmp          # 写入中途的临时文件（正常运行结束后不应存在）
```

## 核心语义说明（为什么旧值不会复活）

构造跨三层覆盖：同一键 `k` 的 `v1` 在 L2、`v2` 在 L1、`v3` 在 L0，随后删除 `k`（墓碑在 L0）。

1. `compact level=0` 合并 L0+L1：墓碑 seq 最大而胜出。此时 L2 仍有 `k=v1`，因此墓碑**保留**（报告 `tombstones_kept=1`），合并结果在 L1。
2. `compact level=1` 合并 L1+L2：墓碑再次胜出，且更老层（L3+）确认无同键，墓碑才**丢弃**（`tombstones_dropped=1`）。
3. 全程点查 `k` 均为 404；墓碑丢弃后所有层都不再有 `k` 的任何版本。
4. 若第 1 步在「段文件写完、manifest 切换前」崩溃：重启恢复的是旧 manifest，墓碑与三个旧版本的可视图原样不变；孤儿新段在启动时删除，随后可重新执行合并。

## 测试

```bash
cargo test                 # 全部单元 + 集成测试
cargo test -- --nocapture  # 显示输出
```

集成测试（`tests/lsm.rs`）覆盖：

- 写入/覆盖/删除、seq 单调、刷盘后重开恢复；
- 跨三层同键覆盖后删除，墓碑「先保留、后丢弃」的完整生命周期，旧值不复活；
- 合并在进程内 panic 中断：已发布清单与可视图不变、孤儿文件落盘、重开清理后继续合并；
- 范围扫描跨层去重、排序、半开区间、limit、墓碑不输出；
- **真实子进程**：启动 HTTP 服务 → 三层覆盖 → 删除 → 注入 `exit(42)` 崩溃 → 两次重启，端到端验证旧值不复活、范围无重复、清单未被失败的合并改写；
- memtable 达阈值自动 flush。

另附自包含的命令行验收脚本（自动启停服务、注入真实崩溃并重启，需要 `curl` 与 `python3`）：

```bash
cargo build --release
bash scripts/acceptance.sh          # 默认端口 3100；PORT=3217 bash scripts/acceptance.sh 可换端口
```

脚本依次完成：三层同键覆盖 → 删除 → 合并中 `exit(42)` 崩溃 → 同目录重启验证旧值不复活/范围无重复/清单未改写 → 恢复后墓碑先保留后丢弃 → 再次重启验证，全部断言通过才退出 0。

## 简化与边界

- 段整体加载并驻留内存，未实现布隆过滤器、块索引与稀疏索引；
- 合并策略为手动 / 按层触发的全量归并，未做大小分层（tiered/leveled 调度）；
- memtable 没有预写日志（WAL）：进程异常退出会丢失尚未 flush 的写入；段发布本身是原子且持久的；
- 单实例、单进程、全操作互斥锁；键值均为 UTF-8 字符串。
