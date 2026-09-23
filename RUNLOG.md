# 运行记录（RUNLOG）

环境：Linux x86_64，Go `go1.27.1 linux/amd64`（仅标准库，无第三方依赖）。
以下命令与结果均为本机实际执行所得。

## 1. 构建 / 静态检查 / 单元与端到端测试

```bash
$ go version
go version go1.27.1 linux/amd64

$ go vet ./...
（无输出，通过）

$ gofmt -l .
（无输出，全部已格式化）

$ go test -count=1 -race ./...
ok  	raftrun/cmd/raftrun	1.548s
ok  	raftrun/internal/raft	1.017s
ok  	raftrun/internal/sim	3.596s
```

随机化安全压力测试为 200 个随机种子（`internal/sim/random_test.go`），
`-short` 时缩减为 8 个。

## 2. 六个固定场景的实际运行结果

```bash
$ go build -o raftrun ./cmd/raftrun
$ for f in basic-replication old-leader-revival crash-restart \
           majority-outage-window majority-unavailable flaky-network; do
    ./raftrun -scenario examples/$f.json
  done
```

| 场景 | 退出码 | 安全断言 | 已提交条目 | 写入归宿统计 | 领导者更迭 |
| --- | --- | --- | --- | --- | --- |
| basic-replication | 0 | passed | 3 | committed×3, notLeader×1 | 1 |
| old-leader-revival | 0 | passed | 4 | committed×4, **accepted×1** | 2 |
| crash-restart | 0 | passed | 5 | committed×5, notLeader×1 | 3 |
| majority-outage-window | 0 | passed | 1 | committed×1, **accepted×1** | 1 |
| majority-unavailable（恢复后） | 0 | passed | 3 | committed×3 | 1 |
| flaky-network（20% 丢包/5% 重复/乱序） | 0 | passed | 7 | committed×7 | 1 |

关键观察：

- **旧领导者复活**：隔离期对旧领导者节点 1 的写入 `c2` 到运行结束仍为
  `accepted`，未进入任何已提交索引；网络愈合后其分叉日志被新领导者
  （更高任期）的条目在索引 2 处截断覆盖，三节点最终日志逐格一致。
- **多数派失联窗口**：节点 2、3 崩溃后，对唯一存活的旧领导者节点 1 的写入
  `c2-no-quorum` 只有本地 `accepted`，三节点已提交索引停在 1；
  多数派恢复后（majority-unavailable 场景）该条目才被确认（该条目仍是
  节点 1 的**当前任期**条目，且期间未发生领导者更替）。
- **重启**：全集群依次崩溃重启后，跨 3 个任期的 5 条日志从磁盘完整恢复，
  并可在新任期继续提交（见各节点 `tmp/run/restart/node*/state.json`）。
- **退出码**：无参数退出码 2（用法错误）；非法 JSON 退出码 1；
  安全断言失败时退出码 3（由 `invariantCheck.passed` 决定）。

## 3. 持久化落盘验证

`crash-restart` 运行结束后三个节点磁盘状态一致（示例）：

```
node1/state.json: term=3 vote=1
  entries: [(1,'SET a 1'),(1,'SET b 2'),(2,'SET c 3'),(2,'SET d 4'),(3,'SET e 5')]
node2/state.json: term=3 vote=1   （同上 5 条）
node3/state.json: term=3 vote=1   （同上 5 条）
```

写入采用 `state.json.tmp` + `rename` 原子替换。

## 4. 开发过程中实际发现并修复的问题（如实记录）

均在自测阶段发现、修复并补了回归测试，最终版本全部通过，**当前无未通过项**。

1. **陈旧选举定时器风暴（功能性 bug）**：初版每次重置选举定时器都向队列
   新塞事件而不失效旧事件，导致无故障网络下 600ms 内换了十几任领导者。
   修复：引入选举 `nonce`，重排即换号，模拟器丢弃过期 nonce 事件
   （`raft.go` / `engine.go`）。回归：`TestBasicReplication` 断言稳定网络下
   恰好 1 任领导者。
2. **数字节点号的崩溃事件被跳过**：`crash`/`restart` 分支对 `s.nodes[0]`
   的 nil 判断错误，使写数字 ID（2/3）的崩溃事件被静默忽略，表现为
   “多数派失联”场景里写入仍被提交。修复 nil 判断后，写入正确停留在
   `accepted`。回归：`TestMajorityUnsafeWindow`。
3. **`"leader"` 引用在分区期选到失联旧领导者**：按节点号顺序取第一个自称
   领导者的节点，分区时会取到被隔离的旧领导者。修复为取**最高任期**的
   在任领导者。回归：`TestOldLeaderRevival` 中 `c3-on-new-leader`
   正确路由到多数派一侧的新领导者节点 2。

## 5. 未实现 / 范围外

成员变更、快照与日志压缩、真实客户端会话与线性化读、真实网络——
按需求明确排除；全部节点在单进程内以离散事件方式模拟。
