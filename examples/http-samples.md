# HTTP 请求样例

以下样例假设服务运行在 `http://127.0.0.1:8080`：

```bash
go run ./cmd/dynpoold -addr 127.0.0.1:8080
```

或一键执行完整场景：`bash examples/demo.sh`（可用 `BASE=http://host:port` 覆盖地址）。

## 1. 创建池

```bash
curl -sS -X POST http://127.0.0.1:8080/v1/pools \
  -H 'Content-Type: application/json' \
  -d '{
    "name": "demo",
    "workers": 3,
    "queue_size": 8,
    "reject_policy": "abort",
    "grace_period": "30s"
  }'
```

- `reject_policy`: `abort` | `discard` | `discard_oldest` | `caller_run`
- `grace_period`: 可选，Go duration（`500ms`、`10s`、`1m`）

## 2. 查看池列表 / 单个池状态

```bash
curl -sS http://127.0.0.1:8080/v1/pools
curl -sS http://127.0.0.1:8080/v1/pools/demo
```

```json
{
  "name": "demo",
  "state": "running",
  "target_workers": 3,
  "active_workers": 3,
  "retiring_workers": 0,
  "queue_len": 0,
  "queue_cap": 8,
  "running_tasks": 0,
  "completed_tasks": 0,
  "rejected_tasks": 0,
  "cancelled_tasks": 0,
  "dropped_tasks": 0
}
```

`state`: `running` | `shutting_down` | `stopping` | `stopped`

## 3. 提交任务

阻塞任务（会一直占用 worker，直到被 release，强制关闭时收到 ctx 取消）：

```bash
curl -sS -X POST http://127.0.0.1:8080/v1/pools/demo/tasks \
  -H 'Content-Type: application/json' \
  -d '{"type":"block","id":"blk1","name":"blk1"}'
# -> 202 {"id":"blk1","state":"accepted","ran":false}
```

定时任务：

```bash
curl -sS -X POST http://127.0.0.1:8080/v1/pools/demo/tasks \
  -H 'Content-Type: application/json' \
  -d '{"type":"sleep","sleep_ms":100}'
# 不指定 id 时返回服务端生成的 id，如 task-4
```

回显任务（完成后可从 GET task 读回 payload）：

```bash
curl -sS -X POST http://127.0.0.1:8080/v1/pools/demo/tasks \
  -H 'Content-Type: application/json' \
  -d '{"type":"echo","id":"e1","payload":"hello"}'
```

队列满（abort）时：

```
HTTP 429
{"id":"","state":"rejected","ran":false,"error":"dynpool: task rejected","rejected":true}
```

## 4. 运行中缩容 / 扩容

```bash
curl -sS -X POST http://127.0.0.1:8080/v1/pools/demo/workers \
  -H 'Content-Type: application/json' \
  -d '{"workers":1}'
```

响应为最新状态。若有 worker 正在执行阻塞任务，`retiring_workers` 立即增加而
`active_workers` 暂时不变——任务结束后才退出。

## 5. 释放阻塞任务

```bash
curl -sS -X POST 'http://127.0.0.1:8080/v1/blocks/demo/release?name=blk1'
# 或
curl -sS -X POST http://127.0.0.1:8080/v1/blocks/demo/release \
  -H 'Content-Type: application/json' -d '{"name":"blk1"}'
```

## 6. 查询任务结果

```bash
curl -sS http://127.0.0.1:8080/v1/pools/demo/tasks/e1
# {"id":"e1","state":"completed","ran":true,"result":"hello"}

curl -sS http://127.0.0.1:8080/v1/pools/demo/tasks/blk1
# 未完成时: {"id":"blk1","state":"pending","ran":false}
# 强制关闭后: {"id":"blk1","state":"completed","ran":true,"error":"context canceled"}
```

## 7. 优雅关闭（排空所有已接收任务）

```bash
curl -sS -X DELETE http://127.0.0.1:8080/v1/pools/demo
```

关闭中再提交会得到 `409`。返回时 `state=stopped`、`completed_tasks` 等于已接收任务数。

## 8. 强制关闭（取消运行中，丢弃排队中）

```bash
curl -sS -X DELETE 'http://127.0.0.1:8080/v1/pools/demo?force=1'
```

```json
{
  "dropped": ["task-5", "task-6"],
  "status": { "state": "stopped", "cancelled_tasks": 2, "dropped_tasks": 2, "...": "..." }
}
```

## 9. 事件查询

```bash
curl -sS 'http://127.0.0.1:8080/v1/pools/demo/events'
curl -sS 'http://127.0.0.1:8080/v1/pools/demo/events?type=task.completed'
curl -sS 'http://127.0.0.1:8080/v1/pools/demo/events?from=42'
```

事件示例（JSON Lines 风格字段，接口返回数组）：

```json
{"seq":1,"time":"2026-09-23T14:00:00Z","type":"pool.created","pool":"demo","new_size":3}
{"seq":2,"time":"...","type":"worker.started","pool":"demo","worker":1}
{"seq":7,"time":"...","type":"pool.resized","pool":"demo","old_size":3,"new_size":1}
{"seq":8,"time":"...","type":"worker.retiring","pool":"demo","worker":2,"reason":"resize"}
{"seq":15,"time":"...","type":"worker.exited","pool":"demo","worker":2}
{"seq":16,"time":"...","type":"pool.shutdown","pool":"demo","old_size":1,"new_size":0}
{"seq":20,"time":"...","type":"pool.stopped","pool":"demo","reason":"graceful"}
```

事件类型：`pool.created` `pool.resized` `pool.shutdown` `pool.stopped`
`pool.force_stopped` `worker.started` `worker.retiring` `worker.exited`
`task.submitted` `task.enqueued` `task.started` `task.completed`
`task.rejected` `task.cancelled` `task.dropped`

## 10. 错误码

| HTTP 状态 | 场景 |
|---|---|
| 400 | JSON 非法、参数非法、未知任务类型 |
| 404 | 池/任务/阻塞名不存在 |
| 409 | 池名冲突；关闭中/已停止仍提交或 resize；阻塞名冲突 |
| 429 | abort 策略下队列满 |
| 500 | 其他内部错误 |
