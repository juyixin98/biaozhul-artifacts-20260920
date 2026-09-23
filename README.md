# Raft 日志复制实验室（确定性模拟器）

用 Go 标准库（`net/http`，无第三方依赖）实现的 **3–5 节点 Raft 日志复制确定性模拟器**，纯后端、无界面。节点之间只走进程内的“内存网络”，**不是生产集群**；时间是整数 tick，随机源（选举超时抖动）按节点独立注入，相同种子/轨迹可逐字节复现。

覆盖：

- **选举**：随机化（但可播种）的选举超时、RequestVote 多数投票、同任期领导唯一；
- **日志复制与冲突回退**：AppendEntries 一致性检查、冲突任期 hint、冲突尾部截断；
- **持久化**：接口抽象（内存实现 + JSON 文件原子写实现），重启后重放已提交前缀；
- **按多数提交**：且遵守 Raft §5.4.2——**只能直接提交当前任期**的条目，旧任期条目随当前任期条目隐式提交；
- **故障注入**：网络分区、节点停机/重启、旧消息延迟投递（quarantine 后重放）；
- **验收**：枚举短故障轨迹，检查①同任期领导唯一 ②各节点已提交前缀互不冲突，并给出**可回放、可收缩的反例**（Figure 8）。

## 目录结构

```
go.mod                         依赖锁定（仅标准库，Go 1.23）
internal/raft/                 Raft 节点核心
  node.go                      选举/复制/提交/回退，tick 驱动、无 goroutine
  storage.go                   Storage 接口：MemoryStorage / FileStorage（tmp+rename 原子写）
  fsm.go                       键值状态机（SET/DELETE/NOOP）
internal/sim/                  内存网络 + 集群外壳
  network.go                   定延迟队列、分区、旧消息隔离区（stale queue）
  cluster.go                   确定性集群；可选真实定时器自动推进（仅 HTTP demo 用）
internal/check/                验收
  runner.go                    故障动作轨迹执行器（动作是纯 JSON 数据，可落盘回放）
  invariants.go                两条安全不变量检查
  enumerate.go                 有界 DFS 枚举 + 状态哈希去重
  shrink.go                    反例贪心收缩
  scenario.go                  确定性 Figure 8 剧本（正确版 & 内置 bug 版）
  testdata/                    已生成的 Figure 8 轨迹固定件
cmd/server/                    net/http 控制面服务
cmd/verify/                    验收 CLI（枚举 + Figure 8 + 回放 + 收缩）
cmd/tracegen/                  生成轨迹 JSON 固定件
examples/                      curl 请求样例
```

## 依赖

- Go 1.23+（开发环境：go1.23.4 linux/amd64）
- 零第三方依赖；`go.mod` 仅声明模块名与 Go 版本，即“锁定依赖”。

## 启动与运行

### 1. 运行全部自动化测试

```bash
go test ./...            # 普通模式
go test -race ./...      # 竞态检测（自动 ticker 与 HTTP 并发）
go test ./... -v         # 查看枚举轨迹数等详细日志
```

### 2. 命令行验收（枚举 + Figure 8 + 反例回放/收缩）

```bash
go run ./cmd/verify                 # 人类可读结果，失败时退出码 1
go run ./cmd/verify --json          # 机器可读报告
go run ./cmd/verify --depth 3 --max-traces 4000
```

### 3. HTTP 控制面服务

```bash
# 自动推进（每 50ms 一个虚拟 tick），3 节点，种子 42
go run ./cmd/server -addr 127.0.0.1:8080 -nodes 3 -seed 42 -tick-ms 50

# 纯手动模式（tick-ms=0，全部通过 POST /v1/tick 推进，最适合教学/回放）
go run ./cmd/server -addr 127.0.0.1:8080 -nodes 5 -tick-ms 0

# 文件持久化：节点状态写入 dataDir/nodeN/raft-state.json，重启进程后恢复
go run ./cmd/server -nodes 3 -data-dir ./tmp/raft-data

# 故意打开 Figure-8 的“旧任期多数即提交”bug，用于演示反例
go run ./cmd/server -nodes 5 -tick-ms 0 -buggy
```

参数：`-addr`、`-nodes(1..5)`、`-seed`、`-tick-ms(0=手动)`、`-data-dir`、`-buggy`。

### 4. 请求样例

完整可执行样例见 [`examples/curl-examples.sh`](examples/curl-examples.sh)。摘要：

```bash
B=http://127.0.0.1:8080

# 查看集群（tick、领导、各节点角色/任期/日志/提交点）
curl -s $B/v1/cluster

# 手动推进 N 个 tick（响应里直接带不变量检查结果，冲突时 HTTP 409）
curl -s -X POST $B/v1/tick -d '{"n":20}'

# 经当前领导提交命令（不指定 node 即自动找领导；也可 "node":3 指定节点）
curl -s -X POST $B/v1/propose -d '{"command":"SET color blue"}'
curl -s -X POST $B/v1/propose -d '{"node":3,"command":"SET ghost stale"}'

# 故障注入
curl -s -X POST $B/v1/nodes/3/partition            # 隔离节点 3
curl -s -X POST $B/v1/nodes/3/partition -d '{"group":"A"}'  # 指定分区组（同组互通）
curl -s -X POST $B/v1/heal                         # 恢复全网连通
curl -s -X POST $B/v1/nodes/2/stop                 # 关机
curl -s -X POST $B/v1/nodes/2/start                # 重启（从存储恢复）
curl -s -X POST $B/v1/nodes/2/restart              # 关+开
curl -s -X POST $B/v1/stale/release                # 投递所有被延迟的旧消息
curl -s -X POST $B/v1/stale/drop

# 枚举短故障轨迹（正确实现应 violations=0；bug 实现应 409）
curl -s -X POST $B/v1/enumerate -d '{"size":3,"maxDepth":3,"maxTraces":4000}'
curl -s -X POST $B/v1/enumerate -d '{"size":5,"buggy":true,"maxDepth":1}'

# 确定性 Figure 8 剧本及其 JSON 轨迹（buggy=true 时返回 409 + 反例）
curl -s "$B/v1/figure8?buggy=false"
curl -s "$B/v1/figure8?buggy=true"

# 回放任意 JSON 轨迹（在全新内存集群上复现，不影响正在演示的集群）
curl -s -X POST $B/v1/replay -d '{
  "size": 3, "actions": [
    {"op":"run","node":20},
    {"op":"proposeLeader","command":"SET k v"},
    {"op":"run","node":8},
    {"op":"partition","node":1},
    {"op":"run","node":20},
    {"op":"heal"},
    {"op":"releaseStale"},
    {"op":"run","node":20}
  ]}'
```

动作词汇（`op`）：`tick` / `run`(node=推进tick数) / `stop` / `start` / `restart` /
`partition` / `setGroup` / `heal` / `propose` / `proposeLeader` / `releaseStale` / `dropStale`。

## 验收方法与实际结果（本次运行如实记录）

运行 `go run ./cmd/verify`（go1.23.4，linux/amd64）：

```
enumeration: 583 traces, 114 distinct states, exhausted=true, violations=0
figure-8 correct implementation: ok=true (final tick 92, leader 5)
figure-8 buggy implementation: counterexample found
  violation: committed-prefix-conflict @ tick 83 index 1
counterexample JSON replay: ok=true kind=committed-prefix-conflict index=1
counterexample shrink: ok=true (116 -> 74 actions)
RESULT: PASS — all acceptance criteria met
```

含义：

1. **枚举 583 条短故障轨迹**（停机/重启/分区/提议/旧消息，3 节点、深度 3、状态去重、搜索未触顶），正确实现 **0 违例**；
2. **Figure 8**：正确实现安全；打开 `-buggy`（旧任期条目复制到多数即提交）后，在 index 1 出现两个不同的已提交条目（n1 提交 t1 的 `x`，n5 在 t4 提交 t2 的 `y`），命中 `committed-prefix-conflict`；
3. **反例可回放**：轨迹是纯 JSON（见 `internal/check/testdata/figure8-buggy.json`），`POST /v1/replay`、`cmd/verify`、测试均可复现同一违例（同 kind、同 index）；
4. **可收缩**：116 个动作贪心收缩到 74 个且仍违例；
5. HTTP 端到端实测：自动选举、多数提交、分区后旧领导未提交条目不进已提交前缀、`releaseStale` 投递旧消息后系统不被破坏、文件持久化整服重启后已提交前缀与状态机恢复、`-race` 全绿。

> 说明：盲目深度受限的随机枚举几乎撞不上 Figure-8 那种“连续两个未提交任期交错”的窄窗口，
> 因此枚举器支持**确定性种子前缀**（`EnumerationConfig.Seeds`），Figure 8 剧本作为种子被
> 确定性纳入；这比假装“随机枚举发现了它”更诚实。

## 模拟假设与边界（未做/非目标）

- 仅内存网络、单进程，**不用于真实部署**；没有真实 RPC、TLS、成员变更、快照/日志压缩、领导者租约或线性化读。
- 持久化是“整状态 JSON + tmp/rename”的教学模型，没有分段 WAL、fsync 策略或并发写控制。
- 提交规则按 Raft 论文：**不**允许直接提交旧任期多数条目；`-buggy` 开关刻意移除该约束以制造可观测反例。
- 网络模型为固定 1-tick 延迟 + 全序/全失连接；不模拟拜占庭行为、消息篡改或部分网络不对称（分区组原语足以表达非对称切分）。
- 枚举是有界 DFS（默认深度 3、轨迹上限可配），**不是完整证明**；它是针对短故障轨迹的系统化测试。
