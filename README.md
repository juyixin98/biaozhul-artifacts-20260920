# trmerge — 测试结果归并服务（纯后端）

把**多分片（shard）测试执行器**发出的事件流，归并成**可重复、确定性**的测试汇总。
Go 实现，本地构建、JSON/HTTP 接口，**不连接任何云平台**；仅运行**用户在请求中显式提供**的夹具命令；
**缓存目录（事件日志）与工作目录（执行夹具）严格分离**。

---

## 1. 要解决的问题

一次测试运行被切到多个执行器分片上并发执行，事件经网络到达，因此会：

- **乱序**：一个重试的结果可能先于首次尝试的开始到达；
- **重复**：同一事件被重投递（at-least-once）；
- **迟到**：执行器崩溃后，一个早已结束的尝试才把结果送到；
- **缺失**：进程被杀 / 超时，某个尝试或某个分片根本没有结果。

归并器必须在这些情况下给出**稳定且诚实**的结论：
**没有确定结果的测试是“未完成（incomplete）”，绝不能被算成通过。**

### 身份模型：尝试 ID 与测试 ID 分离

| 标识 | 含义 | 例子 |
|---|---|---|
| `test_id` | 逻辑测试定义，跨重试稳定 | `billing.charge` |
| `attempt_id` | 该测试的某一次具体执行 | `billing.charge#2` |
| `attempt_no` | 第几次尝试（从 1 开始，单调） | `2` |

一个 `test_id` 可以有多个 `attempt_id`（崩溃重跑、抖动重跑）。
测试级结论由**最新一次尝试**（最大 `attempt_no`）决定，但每次尝试都会保留，便于审计。

### 四种显式状态

- `passed` — 最新尝试明确通过；
- `failed` — 最新尝试明确失败；
- `canceled` — 收到取消请求且该尝试没有终态结果；
- `incomplete` — 运行已结束，但**从未记录到确定结果**（崩溃 / 超时 / 缺失分片 / 缺失测试）。

> **核心保证**：`incomplete` 与缺失项单独计数，绝不并入 `passed`。

此外分片/运行还有生命周期状态：`pending | running | completed | crashed | canceled | finalized`。

---

## 2. 归并语义（关键决策）

1. **事件幂等**：每个事件有 `event_id`（与 `attempt_id` 不同）。重复 `event_id` 标记为 `duplicate`，不重复应用。
2. **迟到结果不覆盖尝试**：
   - 一个尝试有了终态结果后，**不同**的结果不会改写已记录的首个结果（`first_claim` 原样保留），而是记录为 **冲突（conflict）**；
   - 运行 `finalized` 之后到达的非重复事件标记为 **`late`** 并被拒绝，汇总不再改变；
   - 给**旧尝试**的迟到结果不影响测试结论——测试看的是**最新尝试**。
3. **确定性冲突归并**：对同一尝试相互矛盾的终态声明，采用“最坏消息优先”
   （`failed > canceled > passed`），因此一条迟到的通过**永远掩盖不了**失败；且这是基于**声明集合**而非到达顺序，重放任意乱序都得到同一结果。
4. **执行器崩溃 / 重试**：
   - 夹具进程非零退出 → 服务合成 `shard_finished{outcome:"crashed", exit_code}`；
   - 崩溃时仍在进行、没有结果的尝试 → `incomplete`；
   - 分片恢复后重试完成 → 分片最终状态记为 `completed`（`completed > canceled > crashed`），
     但崩溃中丢失的尝试仍诚实记为 `incomplete`，崩溃不会被“洗掉”；
   - 测试若在新尝试中通过 → 测试 `passed`（重试成功）。
5. **缺失即未完成**：清单（manifest）里列出但没有任何事件的测试 → `incomplete`；
   运行结束后从未上报的分片 → `crashed`。
6. **汇总可重复**：相同事件集合，无论到达顺序如何，`GET …/summary` 的 JSON 完全一致
   （键排序 + 集合化优先级，见 `TestDeterministicAcrossShuffles`）。

---

## 3. 目录结构

```
.
├── go.mod / types.go          # 领域类型、事件协议、校验
├── merger.go                  # 归并核心：身份分离、迟到/冲突处理
├── summary.go                 # 确定性汇总快照（四种状态、冲突列表、计数）
├── store.go                   # 事件日志持久化（append-before-apply）、崩溃恢复、/replay
├── runner.go                  # 本地执行器：只执行显式命令、work/cache 分离、解析 JSONL
├── server.go                  # HTTP JSON API（Go 1.22 mux）
├── registry.go                # 运行中执行器注册表（取消用）
├── cmd/trmerge/main.go        # 服务入口
├── examples/
│   ├── fixtures/              # 用户夹具（显式执行的命令）：pass/crash/retry/cancel
│   └── requests/              # 请求样例 JSON
├── scripts/acceptance.sh      # 端到端验收脚本（真实起服务、真实跑夹具）
└── *_test.go                  # 自动化测试（go test）
```

### 缓存与工作目录分离

- `--cache`（默认 `./.trmerge-cache`）：存放每个 run 的 `events.log` 与 `meta.json`，是崩溃恢复的唯一事实来源；
- `--work`（默认 `./.trmerge-work`）：夹具进程的工作目录。

两者**互不相同、互不嵌套**，启动和每次建 run 都会校验，违例直接拒绝
（见 `TestCacheWorkSeparation`、`TestRunnerWorkDirIsSeparateFromCache`）。
夹具再怎么写文件 / `rm -rf`，也碰不到事件日志。

---

## 4. 构建与运行

需要 Go 1.22+（开发环境为 `go1.22.2 linux/amd64`，无第三方依赖）。

```bash
go build ./...
go test ./...
go run ./cmd/trmerge -addr 127.0.0.1:8080 -cache ./.trmerge-cache -work ./.trmerge-work
```

---

## 5. HTTP / JSON 接口

| 方法与路径 | 说明 |
|---|---|
| `GET  /healthz` | 健康检查 |
| `POST /v1/runs` | 创建 run（`mode` 为 `events` 或 `execute`） |
| `GET  /v1/runs` | 列出 run |
| `GET  /v1/runs/{id}` | run 元数据 |
| `POST /v1/runs/{id}/events` | 投递一批事件（乱序/重复均可） |
| `POST /v1/runs/{id}/finalize` | 结束 run（幂等）；之后事件记为 `late` |
| `POST /v1/runs/{id}/cancel` | 请求取消（execute 模式下还会 SIGTERM 夹具） |
| `GET  /v1/runs/{id}/summary` | **确定性汇总** |
| `GET  /v1/runs/{id}/events` | 读取持久化事件日志 |
| `POST /v1/replay` | 不落库重放；可 `shuffle`+`repeats` 验证可重复性 |

### 事件协议（stdout JSONL，或 `/events` 批处理）

`run_started | run_cancel_requested | run_finalized`
`shard_started | shard_finished | shard_warning`
`attempt_started | attempt_result`

`attempt_result.result ∈ {passed, failed, canceled}`；
`shard_finished.outcome ∈ {completed, crashed, canceled}`。
未知 JSON 字段被忽略，必填字段缺失或取值非法会在该事件上返回错误（不影响同批其他事件）。

### 快速体验（events 模式）

```bash
BASE=http://127.0.0.1:8080
RUN=$(curl -s -XPOST $BASE/v1/runs -H 'Content-Type: application/json' \
  -d @examples/requests/create-events-run.json | jq -r .run_id)
curl -s -XPOST $BASE/v1/runs/$RUN/events -H 'Content-Type: application/json' \
  -d @examples/requests/post-events-batch.json
curl -s -XPOST $BASE/v1/runs/$RUN/finalize -d '{}'
curl -s $BASE/v1/runs/$RUN/summary | jq .
```

### execute 模式（服务本地运行**你显式给出**的命令）

```bash
curl -s -XPOST $BASE/v1/runs -H 'Content-Type: application/json' \
  -d @examples/requests/create-execute-run.json
# 每个 command 经 `bash -c` 在独立 work 目录下执行，stdout 按行解析为事件；
# 非零退出自动记为 crashed；auto_finalize=true 时全部退出后自动结束 run。
```

服务**不会**自己臆造或联网拉取任何命令——它只执行请求 `commands` 里给出的命令。

### 乱序/重复重放与可重复性验证

```bash
curl -s -XPOST $BASE/v1/replay -H 'Content-Type: application/json' \
  -d @examples/requests/replay-shuffled.json | jq '{identical, status: .summaries[0].status}'
# => { "identical": true, "status": "failed" }
```

---

## 6. 验收（实际执行）

一键端到端验收（真实编译、起服务、跑夹具、起停恢复）：

```bash
bash scripts/acceptance.sh
```

它覆盖：乱序 + 重复 + 迟到事件、执行器崩溃（真实退出码 137）、崩溃后重试、
取消、缺失测试/分片、finalize 后迟到通过被拒绝、跨重排汇总一致、进程重启后从事件日志恢复。

**实际运行结果记录在 [`RUN_REPORT.md`](./RUN_REPORT.md)**（命令、输出、通过/未通过项如实记录）。

---

## 7. 安全与边界

- 纯本地、无云连接、无第三方依赖；
- 只执行显式提供的命令；execute 模式仍意味着“你允许服务以当前用户身份运行这些命令”，
  因此请只传入可信夹具（本地 CI 工具定位）；
- work/cache 路径分离并校验；请求体大小受限；JSON 严格拒绝未知字段（API 层）；
- 取消先发 SIGTERM、宽限后再 SIGKILL；支持每分片 `timeout`（Go duration）。
