# 实际运行记录（RUN LOG）

- 环境：Linux 6.8.0-90-generic x86_64，Go 1.22.2，无第三方依赖
- 时间：2026-09-23
- 说明：以下命令与结果均为本次开发过程中实际执行所得。

## 1. 构建与静态检查

```
$ go build -o bin/batchagg ./cmd/batchagg      # OK，产物 bin/batchagg (约 7.7MB)
$ go vet ./...                                  # OK，无告警
$ gofmt -l .                                    # 无输出（格式干净）
```

## 2. 自动化测试

完整输出见下方 go test 汇总。结论：**全部通过，无未通过项**。

```
$ go test -count=1 ./...
?   batchagg/cmd/batchagg [no test files]
ok  batchagg       0.334s
ok  batchagg/httpapi 0.945s

$ go test -race -count=3 ./...
?   batchagg/cmd/batchagg [no test files]
ok  batchagg       2.095s
ok  batchagg/httpapi 3.872s
```

用例数：`batchagg` 包 16 个、`httpapi` 包 9 个，共 **25 个测试函数全部 PASS**，
竞态检测连续 3 轮干净。并发压力用例（16 worker × 100 请求 + 随机取消）
一次典型输出：`1459 succeeded, 141 canceled, 304 batches executed`
（取消数因取消与下发的真实竞态而在各次运行间变化，属预期；账目总数恒等于 1600）。

覆盖的验收点：

| 验收要求 | 对应测试 | 结果 |
|---|---|---|
| 低流量超时发批（虚拟时钟） | `TestLowTraffic_WaitFlush` | PASS |
| 高流量满批即发（虚拟时钟） | `TestHighTraffic_ItemsFlush` | PASS |
| 字节阈值触发与跨批拆分 | `TestBytesLimit_FlushesAndSplits` | PASS |
| 批内部分失败、逐项独立结果 | `TestPartialBatchFailure` / `TestSimulator_*` / `TestHTTP_PartialFailureIsolated` | PASS |
| 整批错误逐项独立送达 | `TestWholeBatchErrorFailsEachItem` | PASS |
| 超大单项明确拒绝 | `TestOversizeItem_Rejected` / `TestHTTP_OversizeItemRejectedWith413` | PASS |
| 取消只影响对应项 / 下发后取消无效 | `TestCancellation_AffectsOnlyOwnItem` / `TestCancellation_AfterDispatchDoesNotChangeResult` | PASS |
| 兼容键隔离 | `TestDifferentKeys_NeverMixed` / `TestHTTP_*Key*` | PASS |
| 关停排空（reason=shutdown） | `TestClose_FlushesPending` + 端到端实测 | PASS |
| SSE 结构化事件流 | `TestHTTP_EventsStreamDeliversStructuredEvents` + 端到端实测 | PASS |
| 并发与取消不串扰 | `TestStress_ConcurrentKeysAndCancellations`（-race） | PASS |

## 3. 端到端实测（真实进程 + curl，SystemClock）

启动：

```
$ ./bin/batchagg --addr 127.0.0.1:18091 --max-items 3 --max-bytes 65536 \
                 --max-wait 300ms --event-log /tmp/batchagg_events.jsonl
$ curl -sS http://127.0.0.1:18091/healthz
{"status":"ok"}
```

实测结论（均与预期一致）：

- **A 高流量满批**：3 个同键请求并发，全部立即返回且共享 `batch_id=batch-2`，
  各自 `index` 为 0/1/2，HTTP 均 200（未等待 300ms）。
- **B 低流量超时发批**：单条请求实测等待 **0.311s** 后返回，批原因 `wait`。
- **C 键隔离**：`model-x` 与 `model-y` 两请求分别落在 `batch-4`、`batch-5`。
- **D 批内部分失败**：同键 3 项并发，中间 `fail:true` 项返回
  HTTP 502 `code=simulated_inference_failure`（错误含 `SIM_E42`），
  其余两项 200，三者 `batch_id` 同为 `batch-6`。
- **E 超大单项**：
  - 70000 字符请求（超过 HTTP body 上限 69632）→ 413 `http_body_too_large`；
  - 67028 字节请求（≤ HTTP 上限但 > max-bytes 65536）→ 413
    `oversize_item`，错误信息带实际字节数与限额。
- **F 坏请求**：非法 JSON → 400 `bad_json`；无 key/model → 400 `missing_key`。
- **G 拒绝不影响后续**：超大项拒绝后，同键普通请求立即正常返回 200。
- **H SSE 事件流**：`GET /events` 在两次同键请求期间收到完整事件序列
  `batch_open ×1, submitted ×2, batch_flush ×1, item_result ×2`，字段为结构化 JSON。
- **I 取消隔离**（独立实例，max-wait=2s）：3 项入队后第 2 个客户端
  50ms 断开（curl 退出码 28）；事件日志出现一条 `item_canceled`
  （`items:2, err:context canceled`），另两项 2s 后照常以 2 项成批返回，
  `index` 重排为 0/1。
- **J 关停排空**：见下「开发中发现并修复的问题」。

结构化事件日志（`--event-log` JSON Lines）在主实例上的事件计数：

```
{'batch_open': 8, 'submitted': 13, 'batch_flush': 8, 'item_result': 13, 'rejected': 1}
```

## 4. 开发中发现并修复的问题（如实记录）

1. **runner 关停时未排空已入队消息**：首版 `runKey` 在 `closeCh` 分支直接
   return，导致关停瞬间已进入 channel 缓冲但尚未处理的提交永久挂起，
   `go test` 全量套件因此超时挂死。修复为：收到关闭信号后先 `for len(q)>0`
   排空再以 `shutdown` 发批；由 `TestClose_FlushesPending` 防回归。
2. **Future 语义混淆**：单项执行失败最初同时写 `ItemResult.Err` 和
   `Future.Get` 的 error，使「取消/入队失败」与「独立执行结果失败」无法区分，
   三个测试暴露后，改为：执行结果（含单项失败）一律在 `ItemResult.Err`，
   `Get` 的 error 仅用于「没有结果」（下发前取消，此时 result==nil）。
3. **main 关停顺序导致 drain 退化成 wait**：端到端场景 J 首次实测时，
   先 `srv.Shutdown()` 再 `sched.Close()`，而 HTTP Shutdown 会等待挂起的
   `/infer` 长连接，于是批次总是等到自然 `max-wait` 才以 `wait` 发出
   （30s 配置下会阻塞 30s）。修复为两者并行：监听先关、调度器立即排空，
   挂起处理随结果返回后 HTTP Shutdown 收尾。复测 max-wait=30s 时，
   SIGTERM 后 **0.41s** 即返回 200，事件为 `reason=shutdown`，进程退出码 0。

## 5. 已知边界 / 未做项

- 仅纯后端 + 模拟器执行器；未接入真实推理服务，未做任何前端。
- 模拟器输出为 prompt 回显与元数据，用于驱动调度与结果核对。
- 取消传播依赖客户端 ctx 结束；库本身不提供按 ID 远程取消的独立 API
  （HTTP 层用连接断开表达）。
