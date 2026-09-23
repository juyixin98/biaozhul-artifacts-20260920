# 并发令牌预算（Concurrent Token Budget）

纯后端的**分层令牌桶调度库**与本地 HTTP 接口。一次请求必须**同时、原子地**通过
两层预算（全局层 + 租户层）才会扣减；任一层不足则两层都不消费。调度时钟与
延迟执行器均可替换（真实时钟 / 虚拟时钟），所有状态变更输出**结构化事件**。

- 语言：Go 1.22，仅标准库，无第三方依赖
- 整数算术：全程 `int64` + 手写 128 位乘除，**不使用任何浮点数**
- 单调时钟：补充只按单调纳秒差计算
- 动态速率：随时切换 rate/capacity，切换**绝不凭空增发存量**

---

## 目录结构

```
.
├── go.mod
├── cmd/tokenbudget/main.go       # 本地 HTTP 服务入口
├── internal/
│   ├── clock/clock.go            # 可替换时钟 + 执行器（Real / Fake）
│   ├── event/event.go            # 结构化事件、事件总线、内存/JSONL 落盘
│   ├── budget/
│   │   ├── math.go               # 128 位无符号乘除（bits.Mul64/Div64）
│   │   ├── bucket.go             # 整数令牌桶：突发/补充/预留/动态配置
│   │   ├── limiter.go            # 全局+租户两层，原子扣减
│   │   └── scheduler.go          # 预留 + 延迟执行（执行器可替换）
│   └── httpapi/server.go         # 本地 HTTP 接口
├── examples/config.json          # 示例配置（确定性：固定突发、停止补充）
├── scripts/e2e_demo.sh           # 端到端 curl 演示脚本
└── docs/
    ├── api.md                    # 接口与请求/响应样例
    ├── design.md                 # 设计与正确性说明
    └── e2e_transcript.txt        # 一次真实运行的完整记录
```

## 快速开始

```bash
# 构建
go build -o bin/tokenbudget ./cmd/tokenbudget

# 运行（默认 127.0.0.1:8080；这里用示例配置与自定义端口）
./bin/tokenbudget -addr 127.0.0.1:18080 -config examples/config.json \
                  -events events.jsonl

# 另开终端：一键端到端演示（确定性配置，不依赖墙上时钟）
bash scripts/e2e_demo.sh http://127.0.0.1:18080
```

命令行参数：

| 参数       | 默认值        | 说明                                   |
|------------|---------------|----------------------------------------|
| `-addr`    | `127.0.0.1:8080` | 监听地址（仅本地 HTTP）             |
| `-config`  | 空（内置默认）| JSON 配置路径                          |
| `-events`  | 空            | 可选：把每个事件以 JSON Lines 追加落盘 |
| `-horizon` | `24h`         | 调度允许的最大等待；`0` 表示不限制     |

## 运行测试

```bash
go test ./...                 # 全部 50 个测试
go test -race ./...           # 含竞态检测
go test -v -count=3 ./...     # 重复运行（并发/属性测试）
```

测试使用**虚拟时钟**对纳秒级时刻做精确断言，覆盖：

- 突发容量、按单调纳秒精确补充、亚令牌累积无浮点漂移；
- 动态速率/容量切换不凭空增发、缩容钳制、停止→恢复不补涨；
- 两层原子扣减：全局或租户任一层拒绝时**无部分消费**；
- 预留（firm reservation）在未来锚点上排队，就绪时刻精确到纳秒；
- 高并发下的存量守恒（随机属性测试 + `math/big` 128 位除法预言机）；
- HTTP 接口全路径（`net/http/httptest`）。

## HTTP 接口摘要

| 方法 | 路径                     | 作用                         |
|------|--------------------------|------------------------------|
| GET  | `/healthz`               | 健康检查                     |
| GET  | `/state`                 | 两层各桶当前快照             |
| POST | `/request`               | 立即两层扣减（TryTake）      |
| POST | `/schedule`              | 预留并在就绪时延迟执行       |
| GET  | `/jobs`、`/jobs/{id}`    | 作业列表 / 单个作业          |
| PUT  | `/config/global`         | 替换全局 rate/capacity       |
| PUT  | `/config/tenants/{id}`   | 创建/替换某租户 rate/capacity|
| GET  | `/events`                | 结构化状态变更事件           |

请求/响应字段与完整 curl 样例见 [`docs/api.md`](docs/api.md)；
设计原理与正确性论证见 [`docs/design.md`](docs/design.md)。

## 作为库使用

```go
clk := clock.NewRealClock()
bus := event.NewBus(clk, &event.MemorySink{})
l, _ := budget.NewLimiter(clk, bus,
    budget.Config{Rate: budget.Rate{Num: 100, Den: 1e9}, Capacity: 100}, // global
    budget.Config{Rate: budget.Rate{Num: 10,  Den: 1e9}, Capacity: 20},  // tenant default
)

// 非阻塞：两层都够才扣，否则一层都不动
d := l.TryTake("acme", 3)
if !d.Allowed { /* d.Reason / d.WaitNS */ }

// 阻塞式调度：在“两层都就绪”的时刻原子预留，到时由执行器运行
sched := budget.NewScheduler(l, clk, bus, int64(24*time.Second))
h, err := sched.Submit("acme", "send-email", 1, func() error {
    // 你的业务逻辑；此处只在预算就绪后执行
    return nil
})
<-h.Done()
```

## 速率与令牌的表示

- 速率 = 每 `rate_den_ns` 纳秒产出 `rate_num` 个**整数**令牌；`rate_num=0` 表示停止补充。
- 例：`1 token / 10s` 写成 `{rate_num:1, rate_den_ns:10000000000}`，
  不是 `0.1/s` 这样的浮点近似。
- 亚令牌进度以“分子刻度”余数 `rem ∈ [0, den)` 精确携带，
  经 128 位整数运算在任意多步后仍与一次性计算严格一致。
