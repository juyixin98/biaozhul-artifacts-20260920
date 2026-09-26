# etagrace — 条件更新竞争（Conditional Update Race）

一个**纯后端**教学/验证项目，用 Go 标准库实现基于 **ETag + `If-Match`** 的乐观
并发条件更新，演示并验证"两个相同版本的并发更新只有一个成功"这一核心性质。

- 只依赖 Go 标准库，无第三方依赖
- 本地 HTTP 服务 + 可注入故障的客户端
- 外部依赖（审计下游）用**本进程假服务**模拟，可确定性注入失败与延迟
- 提供**可控时钟**（真实时钟 / 假时钟），测试零真实等待、可复现
- 输出**结构化测试结果**（JSON 验收报告 + 每次请求的结构化记录）
- 不连接任何生产系统，无前端，状态仅存内存

---

## 1. 它解决什么问题

两个客户端基于同一个版本读取资源，然后都想写回。如果不加保护会发生
"丢失更新"。HTTP 的标准解法是乐观并发：

1. `GET` 资源，服务端返回强校验器 `ETag`，例如 `"v2-81b637d8fcd2c6da"`。
2. 客户端写回时带上 `If-Match: "<那个 ETag>"`。
3. 服务端在**同一个临界区**内完成"前置条件判断 → 提交版本/内容"：
   - 匹配：提交新版本，返回 `200` 和新 `ETag`；
   - 不匹配（已被别人改过）：拒绝，返回 `412 Precondition Failed`，
     并在响应头回带当前 `ETag`，供客户端重新读取后重试。

因此并发的两个相同版本更新中，**恰有一个 `200`，另一个 `412`**。

### ETag 设计：版本与内容原子绑定

ETag 形如 `"v<version>-<sha256(content)前8字节>"`，由版本号和内容哈希共同决定，
二者在一次提交中一起变化，不可能出现"版本变了内容没变/反之"的中间态。

- 版本号**单调递增、永不复用**——即使资源被删除后再创建，也分配更高的新版本号，
  因此旧 ETag 在删除重建后**永远不会**再次匹配。
- 强校验器：`"v3-..."`；弱校验器：`W/"v3-..."`。
- 按 RFC 9110 §13.1.1 / §8.8.3，**弱校验器不得用于状态变更请求的强比较**：
  `PUT`/`DELETE` 携带 `W/` 前缀的标签一律 `412`（错误码 `weak_etag_rejected`）。

### "失败不产生副作用"如何保证

写操作在存储层的单把互斥锁内执行：**前置条件检查 → （假）下游通知 → 版本/内容
提交**。下游通知以"提交前钩子"形式在锁内运行：

- 通知成功才提交版本与内容；
- 通知失败（可用 `FaultyNotifier` 确定性注入）则整体中止，版本、内容、ETag
  以及审计日志**全部不变**，服务端在重试耗尽后返回 `503`。

服务端对下游还有带指数退避的重试包装（`RetryingNotifier`），瞬时故障会自动恢复。

---

## 2. HTTP 接口

| 方法 | 路径 | 说明 | 成功 |
|---|---|---|---|
| `POST` | `/resources/{key}` | 创建初始表示（已存在则 `409`） | `201` + `ETag` |
| `GET` | `/resources/{key}` | 读取 | `200` + `ETag` / `404` |
| `PUT` | `/resources/{key}` | 条件更新，**必须**带 `If-Match` | `200` + 新 `ETag` |
| `DELETE` | `/resources/{key}` | 条件删除，**必须**带 `If-Match` | `204` |
| `GET` | `/audit` | 查看本进程假审计日志 | `200` |

状态码语义：

| 状态 | 含义 |
|---|---|
| `200/201/204` | 成功，响应头 `ETag` 为强校验器 |
| `404` | 没有当前表示（`GET`） |
| `409` | `POST` 到一个仍存活的 key |
| `412` | 带了 `If-Match` 但不匹配：版本已变、弱标签强比较、或 `*` 命中已删除资源 |
| `428` | `PUT`/`DELETE` 完全没带 `If-Match`（Precondition Required） |
| `503` | 下游副作用失败且重试耗尽；**资源未被修改** |

`412` 响应头会回带资源**当前**的 `ETag`，客户端据此重新 `GET` 再重试
（compare-and-swap 循环，见 `client.UpdateWithRetry`）。

---

## 3. 目录结构

```
.
├── go.mod
├── internal/
│   ├── clock/      可控时钟：Real（墙钟）/ Fake（Sleep 即时推进，不阻塞）
│   ├── etag/       ETag 构造、解析、强/弱比较（StrongEqual/WeakEqual）
│   ├── store/      互斥锁 + 原子 CAS；版本单调不复用；提交前钩子
│   ├── notifier/   本进程假下游：AuditLog、FaultyNotifier（故障/延迟注入）
│   └── server/     HTTP handler、前置条件校验、412/428/503、退避重试包装
├── client/         故障注入 HTTP 客户端 + UpdateWithRetry(CAS) 结构化结果
├── cmd/
│   ├── server/     可独立运行的本地 HTTP 服务
│   └── verify/     进程内跑全部验收场景，输出结构化 JSON 报告
├── examples/       curl 脚本与 .http 请求样例
└── test-results/   实际运行留存：验收 JSON、go test/vet 输出、curl 输出
```

---

## 4. 如何运行

需要 Go 1.22+（开发与验证环境：go1.22.2 linux/amd64）。

### 4.1 一键跑验收场景（推荐，无需手动起服务）

```bash
go run ./cmd/verify            # 结果打印到 stdout，全部通过退出码 0
go run ./cmd/verify -out report.json
```

它在**同一进程**内用 `httptest` 起服务、用假时钟和可故障假下游，依次运行 6 个
验收场景，输出结构化 JSON。

### 4.2 单元/集成测试（带竞态检测与覆盖率）

```bash
go test -race -cover ./...
```

### 4.3 起真实 HTTP 服务用 curl 调用

```bash
go run ./cmd/server -addr 127.0.0.1:8080
# 另一终端：
bash examples/sample-requests.sh
# 或在支持 .http 的 IDE 中打开 examples/requests.http
```

并发竞争的最小复现（两个请求使用**同一个** ETag）：

```bash
TAG='"v1-xxxx"'   # 替换为 POST/GET 返回的真实 ETag
curl -s -o /dev/null -w "%{http_code}\n" -X PUT $BASE/resources/d \
  -H "If-Match: $TAG" --data-binary alice &
curl -s -o /dev/null -w "%{http_code}\n" -X PUT $BASE/resources/d \
  -H "If-Match: $TAG" --data-binary bob &
wait    # 输出一个 200、一个 412
```

---

## 5. 验收场景与结果

`cmd/verify` 覆盖以下场景（`test-results/verify-report.json` 为实际运行留存）：

| 场景 | 关键断言 | 结果 |
|---|---|---|
| `concurrent_same_version_only_one_wins` | 两个同版本并发更新恰有一个 `200`、一个 `412`；版本只前进一次；只有一条审计事件 | ✅ |
| `delete_and_recreate_never_reuses_version` | 删除 `204`；旧标签再写 `412`；重建得到**更高**版本(v3)且 ETag 不同；旧标签对新资源仍 `412` | ✅ |
| `downstream_failure_leaves_no_side_effect` | 下游持续失败→`503`；无审计事件；版本/内容/ETag 不变；恢复后同标签可成功 | ✅ |
| `wildcard_if_match_star` | 存在时 `PUT/DELETE If-Match:*` 成功；删除后 `*` 写返回 `412` | ✅ |
| `missing_precondition_is_428` | 无 `If-Match` 的 `PUT/DELETE` 均 `428`；资源保持不变 | ✅ |
| `weak_etag_cannot_strong_compare` | `W/` 弱标签→`412 weak_etag_rejected`；强 ETag 不变 | ✅ |

### 实际运行记录（本机，2026-09-25）

```text
$ go run ./cmd/verify
verify: 6/6 scenarios passed        # 退出码 0

$ go test -race -cover ./...
ok  etagrace/client            coverage: 84.9%
ok  etagrace/internal/clock    coverage: 100.0%
ok  etagrace/internal/etag     coverage: 100.0%
ok  etagrace/internal/notifier coverage: 96.4%
ok  etagrace/internal/server   coverage: 80.8%
ok  etagrace/internal/store    coverage: 91.5%

$ go vet ./...                   # 无输出，退出码 0
```

真实 curl 端到端（独立服务，见 `test-results/sample-request-output.txt`）观察到：

- 并发同版本：`alice -> 200`，`bob -> 412`，最终版本 `v2`、内容为单一完整写者正文；
- 删除后重建：`v1` → 删除 → 重建为 `v3`，旧 `v1` ETag 再写 `412`；
- 通配 `*`：存在时 `200/204`，删除后 `412`；
- 缺少前置条件：`PUT/DELETE` 均 `428`；
- 弱标签：`412` 且错误体为 `weak_etag_rejected`。

**未通过项：无。** 开发过程中出现过的失败均已修复并复测通过，主要包括：
初版客户端 CAS 测试依赖 goroutine 调度（不稳定），后改为确定性注入 `412` 的
`RoundTripper`；若干编译期未使用变量 / 多传参数错误，已修正。

---

## 6. 故障注入能力

- **客户端传输层**（`client.FailurePolicy`）：让前 N 个请求在发出前即失败
  （`transport_error`），并可对每个请求加延迟（注入假时钟时不占真实时间）。
- **服务端下游**（`notifier.FaultyNotifier`）：
  - `ArmFailures(n)`：接下来恰好 n 次通知失败（并发下失败总数精确，用 CAS 计数）；
  - `ArmFailures(-1)`：持续失败，直到 `Disarm()`；
  - `Latency`：每次通知推进假时钟模拟延迟。
- **CAS 重试**：`client.UpdateWithRetry` 按 `GET → PUT(If-Match) → 412 则重新 GET`
  循环，每次往返都记录为结构化 `Attempt`（方法、路径、`If-Match`、结果、状态码、
  ETag、耗时、错误）。

---

## 7. 设计说明与边界

- 状态在内存中，重启即失效；服务仅监听 localhost，仅供本地验证，未做鉴权/持久化。
- ETag 的内容哈希把请求体当作不透明字节序列，不做内容协商（故始终签发强校验器）。
- "提交前钩子在锁内执行下游调用"是为了在本进程假依赖下**严格**保证
  "失败零副作用 + 校验/提交原子"。面对真实远程下游时，更贴近生产的做法是
  事务性 outbox / 预留版本号 + 失败回滚；本项目刻意不模拟这些复杂度。
