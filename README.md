# 因果广播缓冲（Causal Broadcast Buffer）

纯后端的**确定性离散事件模拟器**：在单个 Go 进程内模拟若干节点通过不可靠网络进行
因果广播（causal broadcast）。网络可以**丢包、重复、乱序**，不依赖任何真实集群、
网络连接或 goroutine 时序；给定相同输入（含随机种子），运行结果逐位确定、可复现。

## 解决的问题

每个消息携带**向量时钟（vector clock）**描述其因果前驱。接收节点按 Birman 式
因果投递规则处理到达的报文：

```
msgVC[sender] == cur[sender] + 1
msgVC[i]      <= cur[i]        （对所有 i != sender）
```

- 条件满足：立即投递，并把时钟与消息时钟合并；
- 前驱未到：消息进入**有界缓冲**，记录缺失依赖，不投递；
- 前驱补齐：自动按因果顺序**级联释放**缓冲中所有已就绪的消息；
- 重复报文：无论目标消息已投递还是仍在缓冲，只投递一次（exactly-once 投递）；
- 缓冲已满：返回**背压（backpressure）**，拒绝该报文且**不改动任何状态**，
  因此发送方稍后重传同一报文是安全的，不会破坏因果顺序。

运行结束（事件全部排空）后，对每个未投递消息给出诊断：是“从未到达”还是
“卡在缓冲”，缺失哪些前驱，以及沿因果链回溯到的**根因**（例如
`B1 → A1`，根因 `A1@C` 表示 C 永远丢失了 A1）。

## 目录结构

```
cmd/cbcast/          命令行入口（JSON → JSON）
internal/node/       向量时钟判定、节点缓冲状态机、重复抑制
internal/netlink/    不可靠链路：丢包/重复/抖动乱序 + 脚本化规则
internal/sim/        离散事件主循环、事件堆、投递记录、永久缺失诊断
internal/jsonio/     JSON 请求/响应、校验、灵活字段解析
internal/scenario/   内置场景生成器（chain / concurrent）
examples/            手工编写的验收请求样例
examples/results/    上述样例的实际运行输出（随仓库提交）
RUNLOG.md            真实执行的命令、结果与未通过项记录
```

## 构建与测试

```bash
go build ./...                 # 编译
go test ./...                  # 全部自动化测试
go test -race ./...            # 带竞态检测
go test -cover ./...           # 覆盖率
go vet ./...
```

## 运行

```bash
# 用请求文件运行，结果写到文件或标准输出
go run ./cmd/cbcast -in examples/chain.json -out /tmp/chain.out.json
cat examples/loss.json | go run ./cmd/cbcast -in -

# 内置场景生成器（不加 -run 只生成请求；加 -run 直接执行）
go run ./cmd/cbcast -gen chain      -nodes 4 -seed 3
go run ./cmd/cbcast -gen chain      -nodes 4 -seed 3 -loss -run
go run ./cmd/cbcast -gen concurrent -nodes 4 -seed 5 -run
```

退出码：成功 0；请求 JSON 非法或校验失败 1；参数错误 2。错误以
`cbcast: <原因>` 写到 stderr。

## JSON 请求格式

```json
{
  "nodes": ["A", "B", "C"],
  "bufferCap": 16,
  "nodeBuffer": {"C": 1},
  "network": {
    "seed": 7,
    "default": {"baseDelay": 1.0, "jitter": 0.0, "reorderJitter": 0.0,
                "loss": 0.0, "duplicate": 0.0},
    "links": [
      {"from": "A", "to": "C", "link": {"baseDelay": 4.0}}
    ],
    "dropIds":    [{"msgId": "A1", "dst": ["C"]}],
    "holdIds":    [{"msgId": "A1", "dst": ["C"], "delay": 15.0}],
    "delayedIds": [{"msgId": "A1", "dst": ["C"], "delay": [10.0, 12.0]}]
  },
  "broadcasts": [
    {"time": 0.0, "from": "A", "body": "文本负载，不参与因果判断"}
  ],
  "maxTime": 0
}
```

字段说明：

| 字段 | 含义 |
|---|---|
| `nodes` | 节点名列表，顺序即向量时钟下标；至少 1 个，名字唯一 |
| `bufferCap` | 每节点缓冲容量（消息条数），省略默认 1024；每条消息占一个槽 |
| `nodeBuffer` | 按节点覆盖缓冲容量，用于构造小缓冲/背压场景 |
| `network.seed` | 链路随机数种子，相同种子 ⇒ 完全相同的运行 |
| `default` | 默认链路参数；`baseDelay` 默认 1.0 |
| `links[]` | 按 `(from,to)` 覆盖默认参数 |
| `baseDelay` / `jitter` / `reorderJitter` | 时延 = base ± jitter ± reorderJitter（负值截 0）；抖动使后发报文可能先到 |
| `loss` / `duplicate` | 每条报文的独立丢包/重复概率，取值 [0,1]，由种子决定 |
| `dropIds[]` | **强制丢弃**某消息到指定节点的所有副本；`dst` 省略表示全部节点 |
| `holdIds[]` | 在正常副本之外，**额外**注入一个或多个延迟副本（模拟重复迟到） |
| `delayedIds[]` | **抑制正常副本**，仅产生脚本化延迟副本（确定性迟到/重传） |
| `broadcasts[]` | 在 `time` 时刻由 `from` 广播 `body`；消息时钟由发送方已投递状态自动生成 |
| `maxTime` | 可选，>0 时模拟到该时刻即停止；仍在途的报文报 `blocked/in-flight`，而非永久缺失 |

- `holdIds` / `delayedIds` 的 `delay` 可以是单个数字，也可以是数组
  （数组长度即额外副本数）；时延是**相对发送时刻**的偏移。
- `dropIds` 优先级最高：同一消息同时出现在 `dropIds` 与
  `delayedIds`/`holdIds` 时，该报文彻底不出现。
- 消息 ID 形如 `A1`、`B2`：发送节点名 + 该节点第几条广播（从 1 开始）。
  广播时本地立即投递，然后向其他节点各发送一份。

## JSON 响应格式（摘要）

```json
{
  "now": 10.0,
  "complete": true,
  "deliveries": [{"time": 5.0, "node": "C", "msgId": "B1",
                  "clock": {"A": 1, "B": 1}}],
  "trace": [
    {"time": 4.0, "type": "buffered", "node": "C", "msgId": "B1",
     "bufferSize": 1, "bufferCap": 16},
    {"time": 5.0, "type": "deliver",  "node": "C", "msgId": "B1",
     "reason": "cascade", "clock": {"A": 1, "B": 1}}
  ],
  "nodes": [{"name": "C", "clock": {}, "deliveredCount": 0,
             "buffered": [{"msgId": "B1", "sender": "B",
                           "missing": [{"from": "A", "have": 0, "need": 1}]}]}],
  "stats": {"broadcasts": 4, "arrivals": 0, "deliveredNew": 0,
            "duplicates": 0, "bufferedTotal": 2, "releasedFromBuffer": 2,
            "backpressure": 0, "forcedDropped": 1, "networkDropped": 0,
            "inFlightAtCutoff": 0},
  "diagnostics": {
    "permanentMissing": [
      {"node": "C", "msgId": "B1", "state": "buffered",
       "rootCauses": ["A1@C"], "rootChain": ["B1", "A1"]}
    ],
    "blocked": [],
    "causalOrderOk": true,
    "violations": []
  }
}
```

`trace.type` 取值：`broadcast`、`deliver-local`、`deliver`
（`reason=cascade` 表示来自缓冲的级联释放）、`buffered`、`duplicate`、
`backpressure`（含 `bufferSize/bufferCap`）、`drop`
（`reason=forced|loss`）。

`diagnostics`：

- `permanentMissing`：事件排空后仍不可能投递的消息，含
  `state`（`buffered` / `never-arrived`）、逐条缺失依赖、
  `rootChain`（因果回溯链）与 `rootCauses`（终止根因）。
- `blocked`：仅当运行在时间上限截断时出现，状态含 `in-flight`
  （报文还在路上，不构成永久缺失结论）。
- `causalOrderOk` / `violations`：独立依据投递日志回放向量时钟，
  复核是否每一条投递都满足因果规则（正常情况下恒为 `true`）。

## 验收样例（均已实际运行，输出见 `examples/results/`）

| 样例 | 验证内容 |
|---|---|
| `examples/chain.json` | 链式因果广播；脚本化乱序使 B1 先于 A1 到 C、C1 先于 B1 到 D；两者先进缓冲，前驱补齐后级联释放，因果序正确 |
| `examples/concurrent.json` | 三条相互独立的并发消息乱序到达彼此，全部立即投递、零误缓冲；A1 的迟到副本被识别为重复并丢弃 |
| `examples/loss.json` | A1 在 C 处永久丢失；B1 到达 C 后一直缓冲，诊断根因链 `B1 → A1`、根因 `A1@C`；A1 自身报 `never-arrived` |
| `examples/backpressure.json` | C 的缓冲容量为 1：A2 先缓冲，A3 到时背压被拒；A1 迟到后级联释放 A2，A3 的迟到重传副本随后成功投递，顺序为 A1→A2→A3 |

真实执行命令与逐项结果见 [`RUNLOG.md`](./RUNLOG.md)。

## 设计要点与边界

- **确定性**：单一事件堆按 `(时间, 入堆序号)` 排序；所有随机调用按
  “丢包 → 重复 → 各副本时延”固定顺序发生；map 遍历均排序。
- **仅进程内模拟**：没有真实 socket/HTTP/集群；网络“发送”只是向堆里
  放若干到达事件。
- **背压不破坏因果序**：背压与重复判定先于任何状态修改；被拒报文的
  重传走完全相同的接收路径。
- **诊断的保守性**：只有事件堆完全排空后仍缺失才下“永久缺失”结论；
  若设置了时间上限，未处理的在途报文只报 `blocked/in-flight`。
- 本项目不包含任何前端代码或界面，仅有 CLI（stdin/文件 JSON）。
