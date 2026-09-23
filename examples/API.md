# HTTP 请求样例

以下样例假设服务运行在 `http://127.0.0.1:8080`。也可直接运行
`examples/acceptance-demo.sh` 看带颜色分段的完整验收脚本。

## 0. 健康检查 / 全局统计

```bash
curl -s http://127.0.0.1:8080/health
curl -s http://127.0.0.1:8080/stats
```

## 1. 创建分区（幂等）

```bash
# body 可省略，或只给 bufferCap
curl -s -X PUT http://127.0.0.1:8080/partitions/orders \
  -H 'Content-Type: application/json' \
  -d '{"bufferCap":16}'

curl -s http://127.0.0.1:8080/partitions
curl -s http://127.0.0.1:8080/partitions/orders
```

## 2. 提交事件（注入延迟 / 失败 / 超时 / 重试）

```bash
# 普通事件：处理耗时 120ms
curl -s -X POST http://127.0.0.1:8080/partitions/orders/events \
  -H 'Content-Type: application/json' \
  -d '{"payload":{"key":"value"},"delayMillis":120}'

# 每次尝试必失败，最多 3 次，尝试间隔 100ms -> 最终提交 FAILURE 占位
curl -s -X POST http://127.0.0.1:8080/partitions/orders/events \
  -H 'Content-Type: application/json' \
  -d '{"payload":"will-fail","fail":true,"delayMillis":20,
       "maxAttempts":3,"retryDelayMillis":100}'

# 处理需要 3s，但单次尝试 500ms 超时；给 2 次尝试机会
curl -s -X POST http://127.0.0.1:8080/partitions/orders/events \
  -H 'Content-Type: application/json' \
  -d '{"payload":"slow","delayMillis":3000,
       "timeoutMillis":500,"maxAttempts":2,"retryDelayMillis":50}'
```

提交返回 `202`，例如：

```json
{"id":"orders-0","partition":"orders","seq":0,"status":"SCHEDULED","attempts":0,
 "maxAttempts":3,"delayMillis":120,"attemptTimeoutMillis":2000,
 "retryDelayMillis":100,"fail":false,"submittedAt":1727100000000,"inFlight":true}
```

事件 id 规则：`{分区名}-{序号}`，也可以直接用序号查询。

## 3. 查询单个事件

```bash
curl -s http://127.0.0.1:8080/partitions/orders/events/orders-0
```

## 4. 读取有序结果（支持长轮询）

```bash
# 立即返回当前已提交输出
curl -s http://127.0.0.1:8080/partitions/orders/results

# 长轮询，最多等 10s 直到有新提交
curl -s 'http://127.0.0.1:8080/partitions/orders/results?waitMillis=10000'

# 增量读取：只取 seq > sinceSeq 的条目
curl -s 'http://127.0.0.1:8080/partitions/orders/results?sinceSeq=12'
```

结果严格按 `seq` 升序：

```json
{"partition":"orders","results":[
  {"seq":0,"eventId":"orders-0","outcome":"SUCCESS",
   "result":{"handled":true,"attempt":1,"delayMillis":120},
   "attempts":1,"completedAt":1727100000130,"committedAt":1727100000131},
  {"seq":1,"eventId":"orders-1","outcome":"FAILURE",
   "error":"Exception: injected processing failure on attempt 3",
   "attempts":3,"completedAt":1727100000400,"committedAt":1727100000400}
]}
```

## 5. 取消事件（排队中 / 运行中 / 重试等待中）

```bash
curl -s -X POST http://127.0.0.1:8080/partitions/orders/events/orders-4/cancel \
  -H 'Content-Type: application/json' \
  -d '{"reason":"no longer needed"}'
```

- 排队中：直接标记 CANCELLED，不会占用 worker；
- 运行中：中断当前尝试（协作式中断，处理器需响应 interrupt）；
- 已完成（SUCCEEDED/COMMITTED/FAILED）：返回 `409 Conflict`；
- 重复取消：幂等，返回当前 CANCELLED 状态；
- 被取消事件不产生结果条目，其 seq 位置被跳过。

## 6. 错误响应

所有错误均为 JSON：

```json
{"error":{"status":429,"message":"partition buffer full: cap=2 outstanding=2"}}
```

| 场景 | 状态码 |
| --- | --- |
| 分区/事件不存在 | 404 |
| JSON 非法、参数越界、分区名非法 | 400 |
| 取消已结束事件 | 409 |
| 分区在途缓冲已满 | 429 |
| 方法不允许 | 405 |
