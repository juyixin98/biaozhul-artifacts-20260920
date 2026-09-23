# 请求样例（场景 JSON）

每个文件是一次 `raftrun` 模拟运行的输入；`output/` 目录保存对应的结果 JSON
（为便于阅读已关闭 `trace`）。运行方式：

```bash
go run ./cmd/raftrun -scenario examples/<file>.json
```

| 输入文件 | 演示内容 | 关键断言 |
| --- | --- | --- |
| `basic-replication.json` | 无故障网络：选举、连续写入、向跟随者写入被拒 | 3 条写入全部提交，三节点一致，全程仅 1 任领导者 |
| `old-leader-revival.json` | **旧领导者复活 + 日志冲突**：分区隔离旧领导者 → 多数派选新领导者 → 愈合后分叉日志被截断覆盖 | 隔离期写入停留 `accepted`，最终已提交索引内容三节点一致 |
| `crash-restart.json` | 领导者崩溃、追随者重启追平、**全集群依次重启** | 跨 3 任期日志从磁盘完整恢复，重启后可继续提交 |
| `majority-outage-window.json` | 两个节点崩溃，多数派失联窗口内向唯一存活节点写入 | 写入只有 `accepted`，已提交索引停在失联前 |
| `majority-unavailable.json` | 同上但随后恢复多数派 | 挂起写入在恢复后被确认，集群继续可用 |
| `flaky-network.json` | 20% 丢包、5% 重复、50% 抖动乱序下持续写入 | 弱网下安全性不变量始终成立 |

## 最小请求示例

```json
{
  "name": "demo",
  "nodeCount": 3,
  "seed": 1,
  "endTime": 1000,
  "heartbeat": 50,
  "electionTimeout": { "min": 150, "max": 300 },
  "network": { "baseDelay": 5, "jitterPct": 0.1, "lossPct": 0.0, "dupPct": 0.0 },
  "dataDir": "tmp/run/demo",
  "events": [
    { "time": 300, "type": "client", "node": "leader", "client": "w1", "command": "SET k v" }
  ]
}
```

`node` 写 `"leader"` 表示由模拟器把请求路由给当前在任领导者；写 `1/2/3`
则精确指定节点（用于演示向跟随者/已崩溃节点写入时的拒绝行为）。

## 观察消息轨迹

把场景里的 `"trace"` 改为 `true`，结果中会包含按时间排序的 `trace` 数组，
事件 `kind` 为 `send` / `deliver` / `drop` / `crash` / `restart` /
`partition` / `heal`，重复消息以 `"detail":"duplicate"` 标记。
