# HLC Service — 混合逻辑时钟（Hybrid Logical Clock）HTTP 服务

用 Go 标准库 `net/http` 实现的纯后端 HLC 时间戳服务。提供本地事件打戳、接收远端
时间戳合并、物理时钟回拨保护、异常未来漂移限制、逻辑计数器溢出处理，以及不丢精度的
序列化。无前端、无第三方依赖。

HLC 论文：*Logical Physical Clocks*（Kulkarni, Demirbas, Medda 等）。

---

## 1. 它解决什么问题

普通物理时钟（墙钟）会回拨、不同机器之间有偏差；普通逻辑时钟（Lamport）又无法读出
"真实时间"。HLC 把两者结合，时间戳是一个二元组：

```
Timestamp = (physical_ms, logical, node_id)
             物理部分(Unix 毫秒)  逻辑计数器   发戳节点
```

它满足：

- **单节点**：任意两个事件的时间戳严格按发生先后递增（同毫秒内靠 logical 区分）。
- **跨节点**：传递消息时把本地时间戳随消息发出，接收方合并后，新时间戳严格大于
  消息携带的时间戳 —— 从而对**有因果关系**的事件给出全序。
- 物理部分始终接近真实墙钟，可直接用于"大概什么时候发生"。

合并规则（论文）：

- 本地事件：`l'.pt = max(本地.pt, 物理现在)`；物理前进则 logical 归零，否则 logical+1。
- 接收远端 `m`：`l'.pt = max(本地.pt, m.pt, 物理现在)`，再按平局规则取较大的
  logical 并 +1（见 `hlc/clock.go` 中 `merge` 的四个分支）。

---

## 2. 依赖与环境

| 项 | 值 |
|----|----|
| 语言 | Go 1.23（开发与验证使用 go1.23.4 linux/amd64） |
| 第三方依赖 | **无**，仅用标准库（`net/http`、`encoding/json`、`sync`、`time` 等） |
| 依赖锁定 | `go.mod` 固定模块与 Go 版本；因为零第三方依赖，**不需要也不会生成 `go.sum`**（`go mod tidy` 验证为空操作）。要做可复现构建，用同一 Go toolchain 即可 |
| 运行时 | 单静态二进制，无数据库、无外部服务 |

---

## 3. 构建与启动

```bash
# 直接运行
go run .

# 或构建静态二进制后运行
go build -o hlc-server .
./hlc-server
```

启动参数（同时支持环境变量）：

| Flag | 环境变量 | 默认 | 含义 |
|------|----------|------|------|
| `--addr` | `HLC_ADDR` | `:8080` | 监听地址 |
| `--node` | `HLC_NODE_ID` | 主机名 | 节点 ID（不能为空、不能含 `:` 或空白） |
| `--drift` | `HLC_MAX_DRIFT_MS` | `1000` | 允许的远端物理时间最大超前量（毫秒） |
| `--max-logical` | `HLC_MAX_LOGICAL` | `4294967295`（2³²−1） | 逻辑计数器上限 |
| `--overflow-wait` | `HLC_OVERFLOW_WAIT_MS` | `250` | 计数器打满后，等待物理时钟前进的最长毫秒数 |

示例：启动三个节点做跨节点实验

```bash
./hlc-server --addr=:8080 --node=node-1 --drift=1000
./hlc-server --addr=:8081 --node=node-2 --drift=1000
./hlc-server --addr=:8082 --node=node-3 --drift=1000
```

优雅退出：`Ctrl-C` / `SIGTERM`（5 秒优雅关闭）。

---

## 4. HTTP 接口

所有请求/响应均为 JSON（`/v1/receive` 额外接受 `hlc://` 文本格式）。

### `GET /healthz`

健康检查。

```bash
curl -s http://localhost:8080/healthz
# {"node_id":"node-1","physical_now_ms":1700000000123,"status":"ok"}
```

### `POST /v1/tick` — 本地事件

原子地发一个新时间戳并推进时钟。

```bash
curl -s -X POST http://localhost:8080/v1/tick
# {"node_id":"node-1","timestamp":{"physical_ms":1700000000123,
#  "logical":0,"node_id":"node-1","wire":"hlc://node-1/1700000000123:0"}}
```

### `GET /v1/now` — 只读查看

返回**最近一次**发出的时间戳，**不推进**时钟、不读物理时间。

### `POST /v1/receive` — 接收/合并远端时间戳

请求体支持四种形态：

```bash
# 1) 信封 JSON
curl -s -X POST http://localhost:8080/v1/receive \
  -H 'Content-Type: application/json' \
  -d '{"timestamp":{"physical_ms":1700000000000,"logical":7,"node_id":"node-2"}}'

# 2) 裸时间戳对象
curl -s -X POST http://localhost:8080/v1/receive \
  -H 'Content-Type: application/json' \
  -d '{"physical_ms":1700000000001,"logical":3,"node_id":"node-2"}'

# 3) JSON 字符串形式的规范文本
curl -s -X POST http://localhost:8080/v1/receive \
  -H 'Content-Type: application/json' -d '"hlc://node-2/1700000000002:3"'

# 4) 纯文本 body
curl -s -X POST http://localhost:8080/v1/receive \
  --data-binary 'hlc://node-2/1700000000003:3'
```

### `GET /v1/status` — 内部计数与时钟偏差

```bash
curl -s http://localhost:8080/v1/status
```

字段：`last`（最近时间戳）、`physical_now_ms`、`max_drift_ms`、`max_logical`、
`overflow_waits`、`tick_count`、`receive_count`、`drift_rejects`、`skew_ms`
（最近时间戳物理部分 − 当前物理时间）。

### 错误响应与状态码

| 状态码 | code | 触发条件 |
|--------|------|----------|
| 400 | `bad_json` / `invalid_timestamp` | body 非法 JSON、字段缺失、负物理时间、节点 ID 非法、logical 超过本节点上限 |
| 422 | `future_drift` | 远端物理时间超前本地超过 `--drift`（消息被拒绝，**不污染**本地时钟） |
| 503 | `overflow` | 逻辑计数器打满且在 `--overflow-wait` 内物理时钟未能前进 |
| 500 | `internal` | 其他内部错误 |

```json
{"error":"Unprocessable Entity","code":"future_drift",
 "detail":"hlc: remote physical time ... is 3599993ms ahead of local ... (limit 1000ms)"}
```

更多可直接复制的请求样例见 [`examples/requests.sh`](examples/requests.sh)；
一键端到端演示见 [`examples/demo.sh`](examples/demo.sh)。

---

## 5. 关键语义（验收点对应实现）

### 5.1 因果消息时间戳严格递增

发送方在发消息前 `POST /v1/tick`，把返回的 `timestamp` 放进消息；接收方收到时
`POST /v1/receive`。合并结果严格大于消息时间戳，因此沿因果链 `a→b→c` 取到的时间戳
严格递增。三个节点的因果链在单元测试 `TestCausalChainThreeNodes` 与 HTTP 测试
`TestHTTPCausalChainThreeServers` 中断言。

### 5.2 同一毫秒突发

同一物理毫秒内的多个本地事件，physical 不变、logical 依次 +1。并发安全由互斥锁
保证（`-race` 下测试 64×200 goroutine 无重复时间戳）。真实 HTTP 演示中 64 个并发
请求落在同一毫秒时，logical 为连续的 `0..n`。

### 5.3 物理时钟回拨

墙钟倒退时，`物理现在 < 本地已保存的 pt`，时钟继续沿用已保存的较大 pt 并递增
logical，时间戳保持单调；待墙钟追上后自动恢复、logical 归零。见
`TestPhysicalClockRollbackKeepsMonotonicity`、`TestReceiveDuringRollback`
（测试通过注入假时钟控制回拨，**不读取真实时间**）。

### 5.4 异常未来漂移限制

远端 physical 超过本地物理时间 `max_drift`（默认 1000ms）时直接拒绝（422），
拒绝不改变本地状态。边界值"恰好超前 1000ms"会被接受。巨大值（如 2⁶²−1）也只走
拒绝路径，见 `TestHugeFutureValueRejected`。

### 5.5 计数器溢出处理

- 正常同毫秒突发：logical 到上限后，时钟**有界地等待**物理时间前进 1ms，再以新
  physical、logical=0 继续——仍严格大于前一个时间戳。
- 异常情况（远端时间戳 logical 正好顶在上限、且其 physical 遥不可及）：等待受
  `--overflow-wait`（默认 250ms）限制，超时返回 `ErrOverflow`（HTTP 503），
  **绝不无限阻塞**（这一点是开发实测中发现并修复的：早期默认 wait 会睡到一个
  遥远的物理时间，已改为带 deadline，并有回归测试
  `TestReceiveOverflowBoundedWaitDoesNotHang`）。
- 对 `logical = 2⁶⁴−1` 再 +1 的 **uint64 回绕**有专门防护：`merge` 显式报告溢出，
  而不是依赖"`+1` 后比较大小"（回绕会错误地变成 0）。回归测试
  `TestReceiveOverflowAtUint64LimitNoWraparound`。

### 5.6 序列化往返不丢精度

- 时间戳用 `int64`（physical）+ `uint64`（logical）表示；JSON 中按数字精确编解码
  （Go 端无损）。响应额外带 `wire` 字段，规范文本形式为：

  ```
  hlc://<node_id>/<physical_ms>:<logical>
  例: hlc://node-1/1700000000123:4294967295
  ```

- 为基于 IEEE-754 double 的客户端（如浏览器 JS）提供两种无损通道：
  1. 响应里的 `wire` 字符串；
  2. 请求中数字可加引号（`"physical_ms":"...","logical":"..."`），服务端按字符串
     精确解析。

  全量程（physical 到 2⁶³−1、logical 到 2⁶⁴−1）的文本/JSON 往返在
  `TestSerializationRoundTripNoPrecisionLoss` 中断言。

> 测试不依赖真实墙钟做判定：核心算法测试全部注入可控的假物理时钟
> （`WithPhysicalFunc` / `WithWaitFunc`），包括回拨、突发、溢出、遥远未来值。

---

## 6. 自动化测试

```bash
# 全部测试（含竞态检测）
go test -race ./...

# 详细输出
go test -race -v ./...

# 覆盖率
go test -coverprofile=cover.out ./...
go tool cover -func=cover.out
```

实测结果（go1.23.4，linux/amd64）：**26 个测试全部通过，`-race` 无告警**；
覆盖率 hlc 包 79.4%、server 包 86.7%。详见 [RESULTS.md](RESULTS.md)。

测试构成：

- `hlc/hlc_test.go`：同毫秒突发、物理回拨、三类接收合并、三节点因果链、漂移边界
  与巨大值拒绝、两类溢出等待、uint64 回绕回归、并发唯一性、全量程序列化往返。
- `server/server_test.go`：HTTP 严格递增、三服务器 HTTP 因果链、四种请求体形态、
  422/400/503 状态码、状态接口、HTTP JSON 大整数精度与回绕防护。

---

## 7. 端到端演示

```bash
go build -o /tmp/hlc-server .
bash examples/demo.sh                 # 脚本自行拉起 3 个节点（:19190-19192）
# 或自行启动节点后：
HLC_NO_SPAWN=1 bash examples/demo.sh
```

脚本演示：健康检查、64 并发同毫秒突发、跨节点因果链（含同毫秒 logical 爬升）、
1 小时未来漂移 422、边界接受、大整数 JSON/`hlc://` 无损往返、溢出快速 503、
状态计数。真实运行输出见 [RESULTS.md](RESULTS.md)。

---

## 8. 项目结构

```
.
├── go.mod                 # 模块定义（零第三方依赖，故无 go.sum）
├── main.go                # 入口：flag/env 配置、启动与优雅关闭
├── hlc/
│   ├── clock.go           # HLC 核心：Tick/Receive/merge、回拨、漂移、溢出
│   ├── wire.go            # Timestamp、比较/校验、JSON 与 hlc:// 文本序列化
│   └── hlc_test.go        # 核心算法测试（注入假时钟）
├── server/
│   ├── server.go          # net/http 路由与请求解码/错误映射
│   └── server_test.go     # HTTP 验收测试
├── examples/
│   ├── requests.sh        # 可复制的 curl 请求样例
│   └── demo.sh            # 三节点端到端演示脚本
├── README.md
└── RESULTS.md             # 实际测试与运行结果（如实记录）
```

## 9. 设计取舍 / 非目标

- **不提供持久化**：时钟状态在内存，重启以当前墙钟初始化。HLC 用于节点运行期的
  事件排序；如需跨重启可自行在启动前把上次时间戳喂给合并逻辑。
- **单进程单时钟**：一个服务实例就是一个节点；多节点请起多个实例（不同 `--node`
  和端口）。
- **漂移保护只在"接收"路径**：本地墙钟本身错误（整体快/慢）超出本服务职责，需要
  NTP 等外部手段。
- 节点 ID 不参与时间戳先后比较（仅用于溯源）；相同 `(physical, logical)` 视为相等。
