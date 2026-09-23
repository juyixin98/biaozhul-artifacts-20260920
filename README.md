# lsm-kv — 简化 LSM-Tree KV 服务（Rust + Axum）

纯后端 HTTP 服务，演示简化的 LSM 存储引擎：

- **内存表（memtable）**：写入先进内存 `BTreeMap`，按大小阈值冻结为不可变内存表；
- **不可变有序段（segments）**：不可变内存表落盘为 JSON Lines 段文件（按 key 有序、同键仅最新版本）；
- **点查 / 范围扫描**：按 memtable → 不可变内存表 → 段（新 → 旧）顺序取最新版本；墓碑（tombstone）遮蔽旧值；范围扫描跨层合并，**每个键至多出现一次**；
- **版本化合并**：合并时同键取 `seq`（全局单调版本号）最大者；
- **墓碑安全规则**：墓碑只有在**确认所有更老层都不存在同键**后才丢弃；更老层若可能存在同键，墓碑必须保留，防止旧值在未来合并中复活；
- **原子清单发布**：新段先写 `*.seg.tmp` + fsync + rename，再写 `MANIFEST.tmp` + fsync + rename 覆盖 `MANIFEST`，最后 fsync 目录。崩溃时旧清单始终完整；重启后清理未被清单引用的孤儿段。

## 依赖

- Rust（stable，2021 edition；开发使用 1.8x 稳定版即可）
- Linux/macOS（使用了文件 `sync_all`/目录 fsync；Windows 上目录 fsync 语义不同，未测试）
- 无外部数据库、无系统级服务依赖

## 启动

```bash
cargo run --release -- --dir ./lsm-data --port 3000 --memtable-entries 1000
# 或：cargo run
```

参数（均可省略）：

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `--dir` | `./lsm-data` | 数据目录（MANIFEST 与 `segments/`） |
| `--port` | `3000` | 监听 `127.0.0.1:<port>` |
| `--memtable-entries` | `1000` | memtable 条目数阈值，达到后冻结并由后台线程落盘 |

健康检查可直接 `GET /state`。

## HTTP 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/put` | 写入，body `{"key":"k","value":"v"}`，返回 `{"seq":N}` |
| POST | `/delete` | 删除（写墓碑），body `{"key":"k"}` |
| GET | `/get?key=k` | 点查，返回 `{"key","found","value"?}` |
| GET | `/scan?start=a&end=z` | 范围扫描，区间 **[start, end)**，参数可省略（全量），返回 `{"items":[["k","v"],...]}` |
| POST | `/flush` | 立即冻结 memtable 并把全部不可变内存表落盘为段 |
| POST | `/compact` | 合并；body 见下 |
| GET | `/state` | 内部状态（next_seq、段清单、memtable 大小） |

`/compact` body：

```json
{
  "mode": "full",                       // "full"（默认）或 "range"
  "newer_idx": 0, "older_idx": 1,       // range 模式：段下标区间（新 -> 旧，0 为最新段）
  "crash": "after_new_segment_before_manifest"
  // crash 可选：
  //   after_new_segment_before_manifest —— 新段已落盘、MANIFEST 未切换
  //   after_manifest_before_delete_old  —— MANIFEST 已切换、旧段文件未删除
}
```

响应含 `report.input_ids` / `report.new_segment_id` / `report.dropped_tombstones`。
注入崩溃后接口返回 500 且**该进程不应继续使用**（模拟宕机）；重新启动进程即完成恢复。

## 请求样例（curl）

```bash
# 写入（为便于演示，建议用小阈值启动：--memtable-entries 2）
curl -s -XPOST localhost:3000/put    -H 'content-type: application/json' -d '{"key":"k","value":"v1"}'
curl -s -XPOST localhost:3000/put    -H 'content-type: application/json' -d '{"key":"a","value":"1"}'
curl -s -XPOST localhost:3000/flush  # -> 段1：k=v1, a=1
curl -s -XPOST localhost:3000/put    -H 'content-type: application/json' -d '{"key":"k","value":"v2"}'
curl -s -XPOST localhost:3000/put    -H 'content-type: application/json' -d '{"key":"b","value":"2"}'
curl -s -XPOST localhost:3000/flush  # -> 段2：k=v2, b=2
curl -s -XPOST localhost:3000/delete -H 'content-type: application/json' -d '{"key":"k"}'
curl -s -XPOST localhost:3000/put    -H 'content-type: application/json' -d '{"key":"c","value":"3"}'
curl -s -XPOST localhost:3000/flush  # -> 段3：k=墓碑, c=3

# 点查与扫描
curl -s 'localhost:3000/get?key=k'   # {"key":"k","found":false} —— 墓碑遮蔽 v2/v1
curl -s 'localhost:3000/scan'        # {"items":[["a","1"],["b","2"],["c","3"]]} —— k 不重复、不复活

# 合并中断（故障点1）后重启服务，再观察 /state 与 /scan
curl -s -XPOST localhost:3000/compact -H 'content-type: application/json' \
  -d '{"mode":"full","crash":"after_new_segment_before_manifest"}'
# 重启：cargo run ...（同一 --dir），孤儿段被清理，状态回到崩溃前

# 正式全量合并：确认无更老层后墓碑才被丢弃
curl -s -XPOST localhost:3000/compact -H 'content-type: application/json' -d '{"mode":"full"}'
# -> dropped_tombstones: ["k"]；再重启，k 仍 found=false（旧值永不复活）
```

一键演示脚本（自动启动服务、构造三层覆盖+删除、两种崩溃重启、最终校验）：

```bash
./scripts/acceptance.sh /tmp/lsm-accept
```

## 测试

```bash
cargo test
```

测试矩阵（`tests/lsm_core.rs`、`tests/http_api.rs`）：

1. 三层同键覆盖后删除：点查被墓碑遮蔽；
2. 范围扫描跨层无重复、墓碑键不出现、范围边界正确；
3. 只合并最新两层时（更老层有同键）**墓碑不得丢弃**，且旧值不复活；
4. 全量合并（无更老层）墓碑才丢弃，且重启后旧值不复活；
5. 故障点 1（新段落盘、清单未切换）：重启清理孤儿段、回到旧清单；
6. 故障点 2（清单切换、旧文件未删）：重启按新清单清理旧文件；
7. memtable + 三层共四个版本的键，扫描只出现一次；
8. HTTP 全链路（含崩溃注入后重启校验）与 400/500 错误处理。

## 磁盘布局与崩溃恢复

```
<dir>/
  MANIFEST                 # JSON：next_seq/next_id/段清单（新 -> 旧），原子 rename 发布
  segments/
    1.seg                  # JSON Lines，每行 {"key","seq","value":...|null}
    2.seg ...
```

- 段文件与清单都走 `tmp -> fsync -> rename -> fsync(dir)`；
- 重启时以 MANIFEST 为唯一事实来源：未被引用的 `*.seg`（合并中断产物）删除，`*.tmp` 删除；
- 旧段文件在清单发布**之后**才删除——删与不删都不影响正确性（下次启动会被当孤儿回收）。

## 已知限制 / 未完成项

- 段为 JSON Lines 全量读入内存，无索引/布隆过滤器/块压缩，属教学级简化实现；
- 无 WAL：进程崩溃时尚未 flush 的 memtable 写入会丢失（LSM 一般由 WAL 保证；本练习侧重段合并与墓碑）；
- 单进程单写者（`Mutex` 串行化），无多节点；
- 合并由接口手动触发（后台仅有 memtable flush 线程），无大小分层/调度策略。
