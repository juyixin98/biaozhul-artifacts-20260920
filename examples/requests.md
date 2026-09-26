# HTTP 请求样例

服务默认监听：
- 流量地址 `http://127.0.0.1:18080`（可用 `-public` 修改）
- 管理/探针地址 `http://127.0.0.1:18081`（可用 `-admin` 修改）

> 本文所有 `curl` 只访问 localhost，外部依赖是同进程假服务。

## 1. 探针（就绪 / 存活分离）

```bash
# 就绪探针：停机第 1 阶段立即返回 503
curl -i http://127.0.0.1:18081/readyz

# 存活探针：排空期间仍 200，直到 CLOSING 才 503
curl -i http://127.0.0.1:18081/livez
```

## 2. 长请求（调用假外部依赖）

```bash
# 正常长请求（依赖默认无故障）
curl -i http://127.0.0.1:18080/work
```

## 3. 流式请求（SSE）

```bash
# 持续 10 秒的流，每 200ms 一个 tick；停机取消时收到 event: cancelled
curl -N "http://127.0.0.1:18080/stream?duration=10s"
```

## 4. 后台任务（停止接收后不得新增）

```bash
# 受理一个 5 秒的后台任务；停机开始后再调用会得到 503
curl -i -X POST "http://127.0.0.1:18080/bg?duration=5s"
```

## 5. 故障注入（作用于假外部依赖）

```bash
# 依赖延迟 2 秒（用于排空窗口内完成/超时的对比）
curl -i -X POST http://127.0.0.1:18081/fault \
  -H 'Content-Type: application/json' \
  -d '{"latencyMs":2000,"fail":false,"hang":false}'

# 依赖直接返回 500
curl -i -X POST http://127.0.0.1:18081/fault \
  -H 'Content-Type: application/json' \
  -d '{"latencyMs":0,"fail":true,"hang":false}'

# 依赖挂起（不返回），用于强制进入“超时取消”阶段
curl -i -X POST http://127.0.0.1:18081/fault \
  -H 'Content-Type: application/json' \
  -d '{"latencyMs":0,"fail":false,"hang":true}'

# 手动释放所有挂起调用
curl -i -X POST http://127.0.0.1:18081/fault/release
```

## 6. 触发停机（可重复信号）

```bash
# 单个停机信号：四阶段执行，响应体是最终结构化报告
curl -i -X POST http://127.0.0.1:18081/trigger-shutdown

# 一次发送 3 个重复信号（模拟连续 Ctrl-C）：计数并加速推进各阶段
curl -s -X POST "http://127.0.0.1:18081/trigger-shutdown?signals=3"
```

## 7. 停机过程中的实时报告

```bash
# 排空/取消进行中可轮询；拿到的是当时快照
curl -s http://127.0.0.1:18081/report | jq .
```

## 8. 典型人工验收流程（三个终端）

终端 A — 起服务：
```bash
go run ./cmd/server -drain 5s -cancel 2s -reject-window 1s
```

终端 B — 制造在途请求：
```bash
curl -s -X POST http://127.0.0.1:18081/fault -d '{"hang":true}'   # 依赖挂起
curl -N "http://127.0.0.1:18080/stream?duration=60s" &             # 流
curl -s http://127.0.0.1:18080/work &                              # 长请求
```

终端 C — 触发停机并观察：
```bash
curl -s -X POST http://127.0.0.1:18081/trigger-shutdown | jq .
# 观察到：readyz 先 503、livez 排空期仍 200；
# 长请求/流/后台任务在 DRAINING 后被 CANCELLING 明确取消；
# closeOrder 顺序为 admin-listener -> http-client -> fake-dependency。
```
