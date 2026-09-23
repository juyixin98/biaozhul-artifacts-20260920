# Raft 日志复制实验室（确定性模拟器）

纯后端教学项目：用 Go 标准库 `net/http` 实现一个 **3–5 节点、完全确定性** 的
Raft 模拟器，覆盖选举、日志冲突回退、持久化与多数派提交，并通过**枚举短故障
轨迹**（分区 / 重启 / 旧消息延迟）来检验两条核心安全性不变量：

1. **同任期领导唯一**（LeaderUniquenessPerTerm）：任意一个任期至多出现一个领导。
2. **已提交前缀不冲突**（CommittedPrefixImmutable / Agreement）：一旦某条日志在
   某索引被提交，该索引内容永不改变；不同节点同一已提交索引的内容必然一致。

项目同时内置两个**故意写错**的协议变体，枚举器能稳定地为它们找到、保存并
**逐字节重放反例（counterexample）**，而同样的故障轨迹在标准 Raft 上始终通过。

> 这是模拟器，不是生产集群：网络是进程内内存队列，时间是虚拟时钟，随机源可
> 注入；没有 goroutine/timer 参与协议本身（HTTP 层只用 net/http）。

## 目录结构

```
raft/        Raft 节点状态机 + 确定性模拟器（虚拟时钟、内存网络、gob 持久化）
verify/      故障动作、场景、不变量检查器、穷举/模糊枚举器、反例库
httpx/       net/http JSON 接口
cmd/raftlab       HTTP 服务入口
cmd/genscenarios  把内置场景导出为 JSON 文件
scenarios/        导出的场景（标准回归集 + 两个规范反例）
examples.sh       端到端 curl 演示脚本
examples_fmt.py   examples.sh 的输出格式化小工具
```

## 依赖与环境

- Go 1.23+（开发机实测 `go1.23.4 linux/amd64`）
- **零第三方依赖**，仅用标准库；`go.mod` 即“锁定依赖”（`go.sum` 无需生成）。
- 演示脚本需要 `curl` 与 `python3`。

## 启动

```bash
go run ./cmd/raftlab -addr :8080
```

根路径 `/` 返回纯文本接口说明。

运行自动化测试：

```bash
go test ./...            # 普通
go test -race ./...      # 竞态检测（HTTP 层有并发）
go test -v ./verify/     # 查看枚举数量与反例重放
```

导出内置场景 JSON：

```bash
go run ./cmd/genscenarios -out scenarios
```

## HTTP 接口与请求样例

以下样例可直接执行；完整脚本见 `./examples.sh`（真实运行结果见下节）。

### 交互式会话

```bash
# 建集群（nodes 3..5；variant: standard|notermcheck|naive；seed 决定一切时序）
curl -s -X POST localhost:8080/sessions \
  -d '{"nodes":3,"variant":"standard","seed":99,"trace":true}'
# => {"id":"s1","nodes":3,"variant":"standard","seed":99}

# 推进虚拟时间（毫秒），触发首次选举
curl -s -X POST localhost:8080/sessions/s1/advance -d '{"ms":250}'
# => "leader": 2 ...

# 提案（node 省略或为 0 时路由给现任领导；非领导会拒绝）
curl -s -X POST localhost:8080/sessions/s1/propose -d '{"command":"SET k v1"}'
# => {"accepted":true,"leader":2,"node":2}

curl -s -X POST localhost:8080/sessions/s1/advance -d '{"ms":80}'   # 复制+提交

# 故障注入
curl -s -X POST localhost:8080/sessions/s1/partition -d '{"isolate":[2]}'   # 分区
curl -s -X POST localhost:8080/sessions/s1/partition -d '{"groups":[[1],[2,3]]}'
curl -s -X POST localhost:8080/sessions/s1/heal    -d '{}'                   # 愈合
curl -s -X POST localhost:8080/sessions/s1/restart -d '{"node":3}'          # 崩溃+按磁盘恢复
curl -s -X POST localhost:8080/sessions/s1/pause      -d '{"node":3}'       # 暂存入向消息
curl -s -X POST localhost:8080/sessions/s1/resume     -d '{"node":3}'       # 延迟后释放（旧消息）
curl -s -X POST localhost:8080/sessions/s1/pause-from  -d '{"node":2}'      # 暂存出向消息
curl -s -X POST localhost:8080/sessions/s1/resume-from -d '{"node":2}'      # 释放陈旧出向消息
curl -s -X POST localhost:8080/sessions/s1/inject-stale-append \
  -d '{"from":2,"to":3,"term":1,"index":1,"command":"SET k OLD"}'           # 直接注入旧 RPC

# 快照
curl -s localhost:8080/sessions/s1
```

快照含每个节点的 `role/term/votedFor/commitIndex/log/committed/kv`，以及可选
`trace`（election / recv / drop / hold / release-from / restart 等事件流）。

### 场景与枚举（无状态）

```bash
# 取规范反例（故障轨迹 JSON）
curl -s localhost:8080/scenarios/counterexamples

# 重放任意场景
curl -s -X POST localhost:8080/scenarios/run -d @scenarios/ce-stale-old-message-overwrites-committed.json

# 枚举短故障轨迹：穷举深度 depth 的故障宏序列 + fuzz 条随机轨迹
curl -s -X POST localhost:8080/enumerate \
  -d '{"variant":"standard","depth":2,"fuzz":300,"maxCounterexamples":5}'
```

场景 JSON 形如：

```json
{
  "name": "ce-stale-old-message-overwrites-committed",
  "nodes": 3, "variant": "notermcheck", "seed": 99,
  "actions": [
    {"kind":"advance","advanceMs":220},
    {"kind":"pause-from","node":2},
    {"kind":"propose","node":2,"command":"SET k OLD"},
    {"kind":"partition","peers":[2]},
    {"kind":"advance","advanceMs":220},
    {"kind":"propose-all","command":"SET k term2"},
    {"kind":"advance","advanceMs":40},
    {"kind":"heal"},
    {"kind":"resume-from","node":2},
    {"kind":"advance","advanceMs":220}
  ]
}
```

## 实测结果（如实在下记录）

`go test -race ./...` 全部通过（22 个测试函数）：

- `raft/`：选举单领导、同种子可复现、多数派提交、 follower 拒绝提案、冲突截断
  回退、重启保留 term/vote/log 并追平、投票跨任期不重用、低任期陈旧 RPC 被拒、
  被隔离领导无法在无多数派时提交。
- `verify/`：9 条手工故障轨迹在标准变体全部通过；2 条规范反例在错误变体上
  100% 复现、在标准变体上通过、重放得到**同一步同一不变量**的违反。
- `httpx/`：建会话/选举/提案/复制、参数校验、反例端点、枚举端点。

枚举器实测（本机，虚拟时钟，墙钟耗时）：

| 变体 | 穷举轨迹 | 随机轨迹 | 反例 | 耗时 |
|---|---|---|---|---|
| standard, depth=2 | 207 | 300 | **0** | ~0.10s |
| standard, depth=2 | 207 | 1000 | **0** | ~0.24s |
| standard, depth=3 | 2796 | 1000 | **0** | ~0.72s |
| naive, depth=3 | 2281（遇反例提前停止） | 0 | 3 | ~0.41s |

`./examples.sh 127.0.0.1:18181` 真实输出（节选自 `examples-output.txt`）：

```
2                                          # 250ms 后唯一领导 = 节点2
accepted: True / leader node: 2
node 1: role=follower ... commitIndex=2 kv={'k': 'v1'}
node 2: role=leader   ... commitIndex=2 kv={'k': 'v1'}
node 3: role=follower ... commitIndex=2 kv={'k': 'v1'}

# 隔离领导2后，多数派选出更高任期(term2)领导1；分区期内两个领导分属不同任期
leader: 1
node 1: role=leader term=2
node 2: role=leader term=1     # 旧任期领导，看不到多数派；检查器按“任期”判唯一
node 3: role=follower term=2

# 愈合后旧领导下台并追平
node 2: role=follower term=2 commitIndex=3 kv={'k': 'v1'}

# 反例重放（notermcheck 变体）：旧消息覆盖已提交前缀
variant: notermcheck ok: False
 VIOLATION: CommittedPrefixImmutable - node 1 index 2 changed from term=2
            cmd="noop" to term=1 cmd="SET k OLD"
 VIOLATION: CommittedPrefixAgreement - nodes 1 and 3 disagree at index 2 ...

# 同一条故障轨迹在标准 Raft 上
True
```

### 两个规范反例的机理

1. `ce-stale-old-message-overwrites-committed`（对应 `notermcheck` 变体）
   冻结 term-1 领导的**出向**消息 → 分区让多数派选出 term-2 领导并在任期 2
   提交索引 1/2 → 愈合并释放冻结队列，陈旧的 term-1 AppendEntries 迟到。
   标准 Raft 凭“发送方任期 < 当前任期则拒绝”挡下；去掉该检查后，陈旧消息把
   已提交的 term-2 表项截断覆盖 → 已提交前缀被改写。

2. `ce-one-quorum-commit-overwritten`（对应 `naive` 变体，另含
   “只复制到 1 个节点即视为提交”的缺陷）少数派领导把仅复制到一个 follower
   的日志标记为已提交；集群统一后，多数派领导覆盖此前缀，两个节点在同一已
   提交索引上命令不一致。

两个反例都可通过把 JSON 里的 `variant` 改成 `"standard"` 原样重放并通过，
证明差异完全来自被注入的协议缺陷。

## 设计要点

- **确定性**：协议代码没有 goroutine/`time`；模拟器用最小堆事件队列 + 纳秒级
  虚拟时钟，选举超时由注入的 `*rand.Rand` 在 `[150ms, 300ms)` 抖动，网络延迟
  固定 4ms，心跳 60ms。相同 `(seed, 场景)` 必然产生相同轨迹与相同反例步骤。
- **内存网络故障模型**：分区消息直接丢弃；`pause/pause-from` 把入向/出向
  消息 FIFO 暂存，`resume` 时携带旧内容迟到投递；`restart` 丢弃全部易失状态
  （role、commitIndex、状态机），仅从 gob“磁盘”恢复 currentTerm/votedFor/log。
- **选举安全**：候选者拉票前 term+1 并持久化自投；投票要求候选人日志“至少
  一样新”；领导当选即追加一条任期 no-op，用于按 Raft §8 规则间接提交旧任期
  日志。
- **日志一致性**：AppendEntries 的 PrevLog 匹配 + 冲突即截断回退，失败时
  nextIndex 递减快速回探；提交遵循“多数派 + 仅直接提交当前任期表项”。
- **检查器**在每条消息投递后读取节点状态，记录每个节点每个索引“首次观察到
  的已提交值”，并跨节点两两比对共同已提交前缀。

## 范围与未完成项（如实说明）

- 仅模拟内存网络与单机进程，**不是**生产 Raft：无快照/日志压缩、无成员变更、
  无线性化客户端（提案在领导分区窗口可能返回后被覆盖，这是 Raft 本身语义，
  需要注册客户端重试）。
- 枚举器是**有界**短轨迹（宏动作、剪枝、2 万条上限），并非完备证明；它是
  找 bug / 回归工具，depth=3 约 3.8k 轨迹、亚秒级。
- HTTP 会话只存内存，重启进程即丢失；接口无鉴权，仅供本机教学。
- `scenarios/` 下的文件由 `cmd/genscenarios` 生成，可重新生成；勿手改后依赖。
