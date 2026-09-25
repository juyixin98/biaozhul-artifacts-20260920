# 请求样例 (alertfsm)

所有示例假设服务监听 `http://localhost:8080`。时间字段 `ts_ms` / `to_ms` 为
Unix 毫秒。持续时间字段同时接受数字（毫秒）和字符串（如 `"60s"`、`"2m"`）。

## 1. 健康检查与虚拟时钟

```bash
curl -s localhost:8080/health
curl -s localhost:8080/api/v1/clock
```

## 2. 管理规则

```bash
# 创建规则：cpu.usage > 80 持续 60s 触发；恢复到 <=80 持续 30s 解除；
# 120s 没有样本进入 no_data
curl -s -X POST localhost:8080/api/v1/rules \
  -H 'Content-Type: application/json' \
  -d '{
    "id": "cpu-high",
    "metric": "cpu.usage",
    "threshold": 80,
    "direction": "above",
    "pending_for": "60s",
    "recovery_for": "30s",
    "no_data_for": "120s"
  }'

# 低水位规则（低于阈值触发）
curl -s -X POST localhost:8080/api/v1/rules \
  -H 'Content-Type: application/json' \
  -d '{
    "id": "disk-low",
    "metric": "disk.free_gb",
    "threshold": 5,
    "direction": "below",
    "pending_for": 10000,
    "recovery_for": 10000,
    "no_data_for": 60000
  }'

curl -s localhost:8080/api/v1/rules
curl -s localhost:8080/api/v1/rules/cpu-high

# 修改配置：状态立即重置为 ok，并产生一个 reset 通知事件
curl -s -X PUT localhost:8080/api/v1/rules/cpu-high \
  -H 'Content-Type: application/json' \
  -d '{"id":"cpu-high","metric":"cpu.usage","threshold":90,"direction":"above",
       "pending_for":"60s","recovery_for":"30s","no_data_for":"120s"}'

curl -s -X DELETE localhost:8080/api/v1/rules/disk-low
```

## 3. 摄入样本

```bash
curl -s -X POST localhost:8080/api/v1/ingest \
  -H 'Content-Type: application/json' \
  -d '{"samples": [
    {"metric": "cpu.usage", "ts_ms": 1700000000000, "value": 50},
    {"metric": "cpu.usage", "ts_ms": 1700000010000, "value": 95}
  ]}'
```

响应把每个样本分类为 `accepted` / `duplicate`（同 metric+ts 已存在，不累加时长）/
`late`（时间戳早于已评估水位，只存储不参与状态机）。

## 4. 推进虚拟时钟

```bash
# 绝对时间
curl -s -X POST localhost:8080/api/v1/admin/tick \
  -H 'Content-Type: application/json' -d '{"to_ms": 1700000070000}'

# 相对时间（字符串或毫秒）
curl -s -X POST localhost:8080/api/v1/admin/tick \
  -H 'Content-Type: application/json' -d '{"by": "30s"}'
curl -s -X POST localhost:8080/api/v1/admin/tick \
  -H 'Content-Type: application/json' -d '{"by_ms": 30000}'
```

时钟单调递增；倒退请求返回 400。

## 5. 查询状态 / 样本 / 通知事件

```bash
curl -s localhost:8080/api/v1/states
curl -s localhost:8080/api/v1/rules/cpu-high/state

curl -s 'localhost:8080/api/v1/samples?metric=cpu.usage'
curl -s 'localhost:8080/api/v1/samples?metric=cpu.usage&from_ms=1700000000000&to_ms=1700000100000'

# 全部事件（仅状态转换时产生）
curl -s localhost:8080/api/v1/events
# 某条规则；增量轮询；限制条数
curl -s 'localhost:8080/api/v1/events?rule_id=cpu-high&after_id=0&limit=50'
```

事件类型：`firing`（pending→alerting）、`resolved`（recovering→ok）、
`no_data_start`（→no_data）、`no_data_end`（no_data→ok/pending）、
`reset`（配置修改导致的状态重置）。
