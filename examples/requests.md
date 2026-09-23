# 请求样例（curl）

假设服务监听 `127.0.0.1:8080`（默认端口，可用 `-addr` 覆盖）。
所有请求/响应均为 JSON。内置三种执行器：

| type   | 行为                                                        | payload |
|--------|-------------------------------------------------------------|---------|
| `echo` | 立即成功，原样返回 payload 作为结果                         | 任意    |
| `sleep`| 休眠指定毫秒后成功（尊重取消与时钟）                        | `{"ms": 123}` 或纯数字 |
| `flaky`| 前 N 次失败、之后成功，用于观察重试与指数退避              | `{"fail_times": 2, "message": "..."}`（默认失败 1 次） |

提交参数：

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | string | 可选，自定义作业 ID；缺省自动生成 `job-<序号>` |
| `type` | string | 必填，执行器名 |
| `priority` | int | 基础优先级，0–9，数字越大越紧急 |
| `payload` | any | 载荷（对象或字符串） |
| `max_attempts` | int | 最大尝试次数，<=0 用默认值 |
| `backoff_ms` | int | 初始重试退避（毫秒），指数翻倍直到上限 |

---

## 1. 健康检查

```bash
curl -s localhost:8080/healthz
# {"status":"ok"}
```

## 2. 提交立即完成的 echo 作业

```bash
curl -s -XPOST localhost:8080/jobs \
  -H 'Content-Type: application/json' \
  -d '{"id":"a","type":"echo","priority":3,"payload":"hello"}'
```

## 3. 提交一个失败 1 次后重试成功的作业

```bash
curl -s -XPOST localhost:8080/jobs \
  -H 'Content-Type: application/json' \
  -d '{"id":"f","type":"flaky","priority":5,"payload":{"fail_times":1}}'

sleep 1
curl -s localhost:8080/jobs/f
# state=succeeded, attempts=2
```

## 4. 查询单个作业 / 列出全部

```bash
curl -s localhost:8080/jobs/a
curl -s localhost:8080/jobs
```

## 5. 取消作业（排队中 → 立即终态；运行中 → 先 cancel_requested）

```bash
curl -s -XPOST localhost:8080/jobs/a/cancel

# 对已终态作业重复取消 → HTTP 409（幂等冲突，不产生新事件）
curl -i -XPOST localhost:8080/jobs/a/cancel
# 取消不存在的作业 → HTTP 404
curl -i -XPOST localhost:8080/jobs/no-such-id/cancel
```

## 6. 指标

```bash
curl -s localhost:8080/metrics
# {"queued":0,"delayed":0,"running":1,"cancel_requested":0,
#  "succeeded":9,"canceled":0,"failed":0,"total":10}
```

## 7. 结构化事件日志

```bash
# 最近事件（可带 limit / before_seq 翻页）
curl -s "localhost:8080/events?limit=50"

# 实时事件流（Server-Sent Events）
curl -N "localhost:8080/events?stream=1"
```

事件类型：`submitted`、`ready`、`priority_boosted`、`started`、
`succeeded`、`failed`、`retry_scheduled`、`cancel_requested`、
`canceled`、`exhausted`。

---

## 8. 核心验收场景：持续高优先级下，低优先级靠老化运行

先把并发占满（2 个长 sleep，服务默认并发 4，这里用 `-max-concurrency 2`
启动，或提交 4 个占槽作业），再提交一个低优先级、持续注入高优先级：

```bash
# 占满 2 个并发槽
curl -s -XPOST localhost:8080/jobs -d '{"id":"hold1","type":"sleep","priority":9,"payload":{"ms":15000}}'
curl -s -XPOST localhost:8080/jobs -d '{"id":"hold2","type":"sleep","priority":9,"payload":{"ms":15000}}'
# 低优先级作业
curl -s -XPOST localhost:8080/jobs -d '{"id":"low","type":"echo","priority":0,"payload":"low-done"}'
# 持续注入高优先级
curl -s -XPOST localhost:8080/jobs -d '{"id":"h1","type":"echo","priority":9,"payload":"h1"}'
curl -s -XPOST localhost:8080/jobs -d '{"id":"h2","type":"echo","priority":9,"payload":"h2"}'
curl -s -XPOST localhost:8080/jobs -d '{"id":"h3","type":"echo","priority":9,"payload":"h3"}'

# 观察 low 的有效优先级随等待老化（每 1s +1）：0 -> 3 -> 6 -> 9
watch -n1 'curl -s localhost:8080/jobs/low'
```

约 15s 后占槽作业结束：`low` 与 `h1/h2/h3` 有效优先级同为 9，但 `low`
入队更早，按 FIFO 先派发。可在事件流中看到 `low` 的 9 次
`priority_boosted`：

```bash
curl -s "localhost:8080/events?limit=200" | \
  python3 -c 'import json,sys;[print(e["type"],e["job_id"]) for e in json.load(sys.stdin)["events"]]'
```

## 9. 参数校验

```bash
# 未知执行器类型 -> 400
curl -i -XPOST localhost:8080/jobs -d '{"type":"nope","priority":1}'
# 优先级越界（允许 0..9）-> 400
curl -i -XPOST localhost:8080/jobs -d '{"type":"echo","priority":99}'
# 重复 ID -> 409
curl -s  -XPOST localhost:8080/jobs -d '{"id":"a","type":"echo","priority":1}' >/dev/null
curl -i -XPOST localhost:8080/jobs -d '{"id":"a","type":"echo","priority":1}'
```
