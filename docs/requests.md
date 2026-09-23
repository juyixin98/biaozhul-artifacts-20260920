# 请求样例 (Request Examples)

以下样例假定服务运行在 `http://127.0.0.1:8080`，数据目录 `./data`。
启动：

```bash
cargo run --release -- --addr 127.0.0.1:8080 --data ./data
```

完整的可执行版见 [`scripts/acceptance.sh`](../scripts/acceptance.sh)。

## 1. 健康检查与状态

```bash
curl -s http://127.0.0.1:8080/health
# {"ok":true,"status":"ok"}

curl -s http://127.0.0.1:8080/stats | jq .
```

`/stats` 字段：

| 字段 | 含义 |
|---|---|
| `current_version` | 已提交的最新单调版本号 |
| `base_version` | 最新压缩 base 文件覆盖到的版本 |
| `keys` / `live_cells` | 当前键数 / 内存中保留的版本单元格数 |
| `open_transactions` | 活跃事务及其读版本（也会钉住 GC 下界） |
| `snapshots` | 显式快照 pin 及其版本 |
| `files.segments/.bases/.stale` | 磁盘上各类数据文件数 |
| `files.live_bytes/.stale_bytes` | 有效/待删除文件占用字节 |

## 2. 事务：begin → put/delete/get → commit/abort

```bash
# 开启事务，返回事务 id 与固定读版本
curl -s -X POST http://127.0.0.1:8080/tx/begin
# {"ok":true,"read_version":0,"tx":1}

# 缓冲写
curl -s -X POST http://127.0.0.1:8080/tx/1/put \
  -H 'content-type: application/json' \
  -d '{"key":"k","value":"v1"}'

# 事务内读（读己之写；看不到其他事务的新提交）
curl -s -X POST http://127.0.0.1:8080/tx/1/get \
  -H 'content-type: application/json' -d '{"key":"k"}'
# {"found":true,"key":"k","ok":true,"value":"v1"}

# 删除
curl -s -X POST http://127.0.0.1:8080/tx/1/delete \
  -H 'content-type: application/json' -d '{"key":"k"}'

# 提交：成功返回分配到的新版本
curl -s -X POST http://127.0.0.1:8080/tx/1/commit
# {"ok":true,"version":1}

# 中止（丢弃缓冲写）
curl -s -X POST http://127.0.0.1:8080/tx/1/abort
```

## 3. 写写冲突（HTTP 409）

两个从事务同一读版本出发、写同一键的事务，先提交者赢：

```bash
curl -s -X POST http://127.0.0.1:8080/tx/begin            # -> tx 2
curl -s -X POST http://127.0.0.1:8080/tx/begin            # -> tx 3
curl -s -X POST http://127.0.0.1:8080/tx/2/put -d '{"key":"k","value":"A"}' -H 'content-type: application/json'
curl -s -X POST http://127.0.0.1:8080/tx/3/put -d '{"key":"k","value":"B"}' -H 'content-type: application/json'
curl -s -X POST http://127.0.0.1:8080/tx/2/commit         # ok, version:N
curl -s -i -X POST http://127.0.0.1:8080/tx/3/commit
# HTTP/1.1 409 Conflict
# {"error":"write-write conflict on key \"k\"","ok":false}

# 客户端处理：abort 失败者 -> 重新 begin（拿到新读版本）-> 重放写 -> commit
```

## 4. 显式长读者快照

```bash
curl -s -X POST http://127.0.0.1:8080/snapshot
# {"ok":true,"snapshot":1,"version":1}

# 即使键被后续事务覆盖/删除，快照读仍固定在 v1
curl -s -X POST http://127.0.0.1:8080/snapshot/1/get \
  -H 'content-type: application/json' -d '{"key":"k"}'

# 释放后快照失效（404），GC 下界随之推进
curl -s -X POST http://127.0.0.1:8080/snapshot/1/release
```

## 5. 版本回收 / 压缩

```bash
curl -s -X POST http://127.0.0.1:8080/gc | jq .
# {
#   "base_cells": 1,
#   "base_version_before": 0,
#   "base_written": true,
#   "bytes_reclaimed": 180,
#   "files_removed": 3,
#   "horizon": 3,
#   "noop_reason": null,
#   "ok": true,
#   "stale_left": 0
# }
```

`horizon = min(所有活跃事务读版本, 所有显式快照版本, 当前版本)`。
有长读者钉在旧版本时，`horizon` 不前进，`files_removed` 受其限制。

## 6. 故障注入（仅用于验证同步边界）

```bash
# 下一次提交的 fsync 屏障强制失败（一次性，触发后自动解除）
curl -s -X POST http://127.0.0.1:8080/admin/faults \
  -H 'content-type: application/json' \
  -d '{"op":"sync","leave_written":false}'
# op ∈ write | sync | rename | sync_parent | remove
# leave_written=true 时先落盘/改名再报错，用于模拟“调用方未确认但已落盘”

curl -s http://127.0.0.1:8080/admin/faults        # 查看当前 armed 故障
curl -s -X POST http://127.0.0.1:8080/admin/faults/reset
```

预期行为：提交在故障点返回 500（`injected fault: ...`），不发布版本、
不推进版本号、事务保持打开，可立即重试提交；重启时残留的
`.tmp` 文件被清理。
