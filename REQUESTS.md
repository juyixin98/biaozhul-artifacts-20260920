# HTTP 请求样例

启动服务（默认 :8080，下列样例使用 :18080）：

```bash
go run ./cmd/agingd -addr :18080 -age-interval 1s -concurrency 1 \
  -events-file events.jsonl
```

## 1. 健康检查

```bash
curl -s localhost:18080/health
# {"status":"ok"}
```

## 2. 提交作业

立即执行的空作业（优先级 3）：

```bash
curl -s -X POST localhost:18080/jobs \
  -H 'Content-Type: application/json' \
  -d '{"type":"noop","priority":3}'
```

延迟 2 秒后才可调度：

```bash
curl -s -X POST localhost:18080/jobs \
  -H 'Content-Type: application/json' \
  -d '{"type":"noop","priority":1,"delay":"2s","id":"late-1"}'
```

带执行载荷的 echo（处理器会 sleep 指定时长）：

```bash
curl -s -X POST localhost:18080/jobs \
  -H 'Content-Type: application/json' \
  -d '{"type":"echo","priority":5,"payload":{"delay":"50ms"},"maxAttempts":2}'
```

观察重试：flaky 前 2 次失败、第 3 次成功：

```bash
curl -s -X POST localhost:18080/jobs \
  -H 'Content-Type: application/json' \
  -d '{"type":"flaky","priority":4,"payload":{"failTimes":2},"maxAttempts":3,"id":"f1"}'
```

## 3. 老化插队场景（核心验收）

单 worker 下，先用一个长 echo 占住 worker，再注入低优先级和持续的高优先级作业：

```bash
# 占住 worker 12 秒
curl -s -X POST localhost:18080/jobs -d \
  '{"type":"echo","priority":9,"payload":{"delay":"12s"},"id":"anchor","maxAttempts":1}'

# 低优先级作业：每 1s 老化 +1，12s 内从 0 升到 9
curl -s -X POST localhost:18080/jobs -d '{"type":"noop","priority":0,"id":"low"}'

# 持续注入的高优先级作业
for i in 1 2 3; do
  curl -s -X POST localhost:18080/jobs -d "{\"type\":\"noop\",\"priority\":9,\"id\":\"high$i\"}"
done
```

观察老化过程（有效优先级随等待时间上升）：

```bash
watch -n1 'curl -s localhost:18080/jobs/low'
```

锚点结束后，`low` 因老化到 9 且 FIFO 提交更早，先于 high1/2/3 运行。

## 4. 查询

```bash
curl -s localhost:18080/jobs/low            # 单个作业
curl -s localhost:18080/jobs                # 全部作业
curl -s localhost:18080/stats | jq .        # 队列深度（按有效优先级分桶）
```

## 5. 取消（含重复取消）

```bash
curl -s -X POST localhost:18080/jobs -d '{"type":"noop","priority":1,"id":"vic"}'

curl -s -X POST localhost:18080/jobs/vic/cancel        # 200，已取消
curl -s -i -X POST localhost:18080/jobs/vic/cancel     # 409 already terminal
curl -s -i -X POST localhost:18080/jobs/ghost/cancel   # 404 not found
```

## 6. 事件

```bash
curl -s localhost:18080/events | jq .          # 内存 Sink 快照
curl -s -N 'localhost:18080/events?stream=1'   # SSE 实时流（另开终端提交作业观察）
jq -c 'select(.jobId=="low")' events.jsonl     # 文件 Sink 中某作业的轨迹
```

## 7. 错误响应

```bash
curl -s -i -X POST localhost:18080/jobs -d '{"priority":1}'          # 400 缺 type
curl -s -i -X POST localhost:18080/jobs -d '{"type":"x","priority":99}' # 400 越界
curl -s -i -X POST localhost:18080/jobs -d '{bad json'                # 400 非法 JSON
# 未知作业类型：作业被接受但立即 failed（fatal，不重试）
curl -s -X POST localhost:18080/jobs -d '{"type":"nope","priority":5,"maxAttempts":5}'
```
