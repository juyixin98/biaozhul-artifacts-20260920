# logpipe — 多行日志归并后端（纯后端 / 合成数据）

一个用 Go 实现的可观测性数据处理样例后端：通过 HTTP 接收逐行日志，
按**起始行规则**把堆栈等多行日志拼装成完整条目，**每个来源独立缓存、
互不混合**，在超时、字节上限、进程重启等情况下截断并显式保留完整性
标志，最后把结果持久化到本地 JSONL 文件，重启后仍可查询。

无前端、无外部依赖、无真实监控平台；所有数据均为合成数据。

## 它解决什么问题

日志采集端按行推送数据，而一次异常往往是多行的：

```
2026-09-24T12:00:00 ERROR payment failed
    at chargeCard (payment.go:88)
    at checkout (checkout.go:142)
```

服务必须决定：

- 哪一行算**一条**新日志的开始（可配置正则，默认匹配 RFC3339 时间戳）；
- 后续的续行归并到**同一来源**正在组装的条目里；
- 两个来源的行交错到达时不能串行（`alpha` 的帧不能进 `beta` 的条目）；
- 等不到下一条起始行时，超时也要刷出（标记为完整，原因 `timeout`）；
- 单条日志超过字节预算时截断（`complete=false, truncated=true`），
  之后到达的续行只计数丢弃，不污染其他来源；
- 来源进程重启（pid 变化）时，旧条目无法再被闭合，立即以
  `complete=false, reason=restart` 刷出；
- 没有起始行的“流浪续行”单独成条（`reason=orphan`），不被静默吞掉；
- 服务关停时未闭合的条目以 `reason=shutdown` 落盘。

## 目录结构

```
cmd/server/main.go        HTTP 服务入口（flags、优雅关停）
cmd/simulator/main.go     合成数据 + 自验证验收程序
internal/merger/          多行归并核心（起始行规则、按来源缓存、截断/重启/孤儿）
internal/store/           JSONL 追加持久化 + 重放 + 查询
internal/server/          HTTP 路由：/ingest /entries /admin/force-flush /healthz
examples/                 curl 请求样例（单条、批量、重启）
scripts/demo.sh           一键端到端演示（构建→运行→模拟器→重启验证）
RUNLOG.md                 实际执行的命令与结果记录
```

## 快速开始

需要 Go 1.22+（仅用标准库）。

```bash
go build ./...
go test ./...              # 单元/HTTP 测试
go test -race ./...        # 竞态检测

# 一键演示（自动选空闲端口，结束后打印临时数据目录）
./scripts/demo.sh
```

手动运行：

```bash
go run ./cmd/server -addr :18080 -data-dir ./data \
  -timeout 5s -max-bytes 4096 -sweep-interval 250ms
```

### 服务参数

| flag | 默认值 | 说明 |
|---|---|---|
| `-addr` | `:8080` | 监听地址 |
| `-data-dir` | `./data` | `entries.jsonl` 所在目录，启动时重放 |
| `-start-pattern` | `^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}` | 起始行正则（匹配即开启新条目） |
| `-timeout` | `5s` | 条目空闲多久后由后台扫描刷出 |
| `-max-bytes` | `4096` | 单条组装结果的字节预算 |
| `-sweep-interval` | `250ms` | 超时扫描周期 |
| `-shutdown-timeout` | `5s` | HTTP 优雅关停最长等待 |

## HTTP API

### `POST /ingest`

请求体可以是**单个对象**或**数组**。每个元素：

| 字段 | 必填 | 说明 |
|---|---|---|
| `source` | 是 | 日志来源标识；不同来源的缓存完全独立 |
| `text` | 是 | 一行日志文本（不含换行） |
| `pid` | 否 | 来源进程 id；同一来源 pid 变化即视为进程重启 |
| `ts` | 否 | RFC3339 时间戳；缺省使用服务端接收时间 |

```bash
curl -X POST localhost:18080/ingest -H 'Content-Type: application/json' \
  --data @examples/ingest-batch.json
# -> {"accepted":8}
```

非法 JSON、空 source、空 text 返回 `400 {"error": ...}`，请求体上限 4MiB。

### `GET /entries?source=<id>&limit=<n>`

按 id 升序返回已组装并持久化的条目。`source` 精确过滤（省略=全部），
`limit` 返回最后 n 条。

### `POST /admin/force-flush`

立即把所有来源挂起中的条目刷出（`complete=false, reason=forced`），
便于测试或同步提交。

### `GET /healthz`

`{"status":"ok"}`。

## 条目模型与完整性语义

```json
{
  "id": 9,
  "source": "epsilon",
  "start_time": "2026-09-24T04:03:47.811803466+08:00",
  "end_time":   "2026-09-24T04:03:47.811803466+08:00",
  "text": "2026-09-24T... INFO EPSILON-NEW post-restart start\n    at epsilon.new (epsilon_new.go:2)",
  "line_count": 2,
  "bytes": 109,
  "complete": true,
  "reason": "timeout",
  "truncated": false,
  "dropped_lines": 0,
  "dropped_bytes": 0
}
```

`reason` 取值与完整性含义：

| reason | complete | 触发条件 |
|---|---|---|
| `next_start` | true | 同一来源的下一条起始行到达，正常闭合 |
| `timeout` | true | 空闲超过 `-timeout`，由扫描器刷出 |
| `orphan` | false | 该来源没有挂起条目时到达的续行，单独成条 |
| `restart` | false | 同一来源 pid 变化，旧条目不可能再闭合 |
| `bytes_limit` | false | 文本达到字节预算；`truncated=true`，后续续行计入 `dropped_*` |
| `forced` | false | 手动 `/admin/force-flush`（是否真完整只有调用方知道） |
| `shutdown` | false | 服务关停时仍挂起的条目 |

字节上限的处理保证：输出 `bytes <= max-bytes`；截断是 UTF-8 rune 安全
的（不会把一个多字节字符切成半个，有专门单测）；溢出后同来源的续行只
增加 `dropped_lines/dropped_bytes`，绝不会泄漏到其他来源或后续条目。

## 持久化

- 每条组装结果以一行 JSON 追加到 `<data-dir>/entries.jsonl`，
  写入后 `fsync`；
- 启动时重放整个文件重建内存索引与 id 序列（重启后 id 继续递增）；
- 末尾若有崩溃导致的半行（无法解析），计数后跳过，不影响历史数据。

## 验收场景（合成数据，自验证）

`cmd/simulator` 会交错推送两个来源的多个堆栈，并覆盖全部异常场景，
随后查询并**逐条断言** source / complete / reason / line_count /
truncated / dropped 字段以及“条目文本里不含任何其他来源标记”：

1. **交错两个来源堆栈** — `alpha`、`beta` 逐行交错，各有多条堆栈，
   验证按来源独立缓存、无跨来源混合、`next_start`/`timeout` 闭合；
2. **无起始行** — `gamma` 先到一条续行，断言 `orphan` 且不完整；
3. **超长异常** — `delta` 单行超过预算，断言 `bytes_limit`、
   `truncated`、`bytes<=预算`、溢出后的续行被丢弃；
4. **进程重启** — `epsilon` 的 pid 从 5005 变为 5006，断言旧条目
   `restart` 不完整，新进程的新条目独立组装并正常超时闭合。

```bash
go run ./cmd/server -timeout 1s -sweep-interval 100ms -data-dir /tmp/lp &
go run ./cmd/simulator -wait 2s
```

实际执行记录见 [RUNLOG.md](./RUNLOG.md)。

## 设计说明与边界

- 归并器持锁时间只覆盖 map/缓冲体内存操作；持久化在锁外的回调中执行，
  HTTP 多请求并发摄入安全（`go test -race` 验证）。
- 时间戳由调用方提供（HTTP 层缺省为服务端时间），核心库可用注入时钟，
  因此超时逻辑可做确定性单测。
- 这是样例后端：单文件 JSONL 适合演示与中小数据量；分片、压缩、
  分页查询、鉴权不在范围内。
- 超时扫描为轮询模型（周期 `-sweep-interval`），因此实际刷出时间是
  “超时 + 至多一个扫描间隔”；对演示与日志场景足够，不提供亚毫秒保证。
