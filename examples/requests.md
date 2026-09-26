# 请求样例 (Request Samples)

所有示例假设服务监听 `http://127.0.0.1:8080`（默认）。
服务不依赖任何真实外部系统：数据库与队列均为进程内假服务（见 `internal/fakesvc`）。

## 端点一览

| 方法 | 路径       | 说明 |
|------|-----------|------|
| GET  | `/healthz` | 存活探针：停机期间始终返回 200，直到进程退出 |
| GET  | `/readyz`  | 就绪探针：接收阶段返回 200；进入排空阶段立即返回 503 |
| GET  | `/work`    | 长请求：向假 DB 发起一次带延迟的查询 |
| GET  | `/stream`  | 流请求：按间隔输出多个 chunk |
| POST | `/task`    | 提交一个后台任务（假队列发布） |
| POST | `/shutdown`| 触发四阶段停机（幂等，可重复调用） |
| GET  | `/state`   | 结构化停机状态（阶段、台账、关闭顺序） |

## 1. 探针（启动就绪与存活分离）

```bash
curl -i http://127.0.0.1:8080/healthz
# HTTP/1.1 200 OK  -> {"status":"alive"}

curl -i http://127.0.0.1:8080/readyz
# 接收阶段: HTTP/1.1 200 OK -> {"status":"ready"}
# 停机后:   HTTP/1.1 503 ... -> {"status":"not-ready","phase":"draining"}
```

## 2. 长请求（应在排空窗口内完成）

```bash
# ms 为假 DB 查询延迟（毫秒），q 为查询内容
curl -i "http://127.0.0.1:8080/work?ms=500&q=hello"
# 200 -> {"outcome":"completed","result":"result(hello)"}
```

## 3. 超时长请求（排空超时后被明确取消）

```bash
curl -i "http://127.0.0.1:8080/work?ms=30000&q=stuck"
# 499 -> {"outcome":"cancelled","reason":"context canceled"}
```

## 4. 流请求（排空窗口内完成 / 中途被取消）

```bash
# 4 个 chunk，每个间隔 150ms（约 600ms）
curl -N "http://127.0.0.1:8080/stream?chunks=4&interval_ms=150"
# chunk 1/4 ... chunk 4/4   -> 完成

# 100 个 chunk，每个 100ms（远超排空窗口）
curl -N "http://127.0.0.1:8080/stream?chunks=100&interval_ms=100"
# chunk 1/100 ... chunk N/100 cancelled: context canceled
```

## 5. 后台任务（停止接收后不得新增）

```bash
curl -i -X POST "http://127.0.0.1:8080/task?msg=job1"
# 停机前: 202 -> {"outcome":"accepted","task_id":2}

curl -i -X POST "http://127.0.0.1:8080/task?msg=late"
# 停机后: 503 -> {"error":"server is shutting down; no new background tasks", ...}
```

## 6. 触发停机（可重复，幂等）

```bash
curl -i -X POST http://127.0.0.1:8080/shutdown
curl -i -X POST http://127.0.0.1:8080/shutdown   # 重复信号，计数但不重复执行
```

或使用操作系统信号（效果相同）：

```bash
kill -TERM <pid>
kill -TERM <pid>   # 重复停机信号
```

## 7. 停机后的拒绝行为

```bash
curl -i "http://127.0.0.1:8080/work?ms=1"
# 503 -> {"error":"server is shutting down; not accepting new work","phase":"draining"}
```

## 8. 结构化状态（验收依据）

```bash
curl -s http://127.0.0.1:8080/state | jq .
```

关键字段：

- `phase`: `receiving | draining | cancelling | closing | done`
- `phase_log`: 实际经历的阶段及时间戳
- `drain_timed_out`: 排空阶段是否超时
- `shutdown_signals`: 收到的停机信号次数
- `rejected_requests` / `rejected_tasks`: 停止接收后被拒绝的新工作
- `close_order`: 资源关闭顺序（LIFO：`fake-queue` → `fake-db`）
- `ledger`: 每个已接收工作单元的终态（`completed` 或 `cancelled`）
