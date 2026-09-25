# API 请求样例（curl）

以下示例假设服务运行在 `http://127.0.0.1:8080`。样例 JSON 文件就在本目录，
可直接 `-d @文件` 提交。完整脚本见 `demo.sh`。

## 1. 乱序到达：子/孙先到，根后到

```bash
# 先到的只有 checkout（父=gateway 缺失）和 pay（父=checkout 已在）
curl -s -X POST http://127.0.0.1:8080/v1/spans \
  -H 'Content-Type: application/json' \
  -d @01-out-of-order-children.json

# 查询：complete=false，latestRevision.missingRoot=true，hasOrphans=true
curl -s http://127.0.0.1:8080/v1/traces/demo-ooo | jq .
```

## 2. 重复与冲突：同 id 不同载荷，首条为准

```bash
# pay-svc 已存在，这里 name/时间戳都被篡改 -> status=conflict
curl -s -X POST http://127.0.0.1:8080/v1/span \
  -H 'Content-Type: application/json' \
  -d @03-duplicate-conflict.json
# => {"status":"conflict","reason":"conflict","differingFields":["name","startUnixNano","endUnixNano"], ...}

# 完全相同的重发（把 01 再发一次）-> status=duplicate，不产生新修订
curl -s -X POST http://127.0.0.1:8080/v1/spans \
  -H 'Content-Type: application/json' \
  -d @01-out-of-order-children.json
```

## 3. 缺根超时：输出不完整修订

```bash
# 方式 A：确定性强制密封（demo/自动化用，不依赖等待）
curl -s -X POST http://127.0.0.1:8080/admin/traces/demo-ooo/flush | jq .
# => sealed=true，latestRevision.reason="timeout"，complete=false，missingRoot=true

# 方式 B：真实壁钟。启动时给短超时：-trace-timeout 1s -sweep-interval 200ms，
#         缺根 trace 在约 1s 后被后台 sweeper 自动密封；也可手动触发扫描：
curl -s -X POST http://127.0.0.1:8080/admin/sweep
```

## 4. 根迟到：新修订补全

```bash
curl -s -X POST http://127.0.0.1:8080/v1/span \
  -H 'Content-Type: application/json' \
  -d @02-late-root.json
# => status=accepted, reason=late, revision=5

# 逐版包含关系必须成立
curl -s http://127.0.0.1:8080/v1/traces/demo-ooo/containment
# => {"holds": true, "traceId": "demo-ooo"}

# 最终结构按引用拼成 gateway -> checkout-svc -> pay-svc
curl -s http://127.0.0.1:8080/v1/traces/demo-ooo | jq '.forest'
```

## 5. 跨服务时钟偏差：只告警，不断因果

```bash
curl -s -X POST http://127.0.0.1:8080/v1/spans \
  -H 'Content-Type: application/json' \
  -d @04-clock-skew.json

curl -s http://127.0.0.1:8080/v1/traces/demo-skew | jq .
# complete=true（偏差不是结构缺陷）
# forest[0] = api，其 child = inventory（因果只看 parentSpanId）
# latestRevision.clockSkew[0].kind = "child-starts-before-parent"
```

## 6. 父链循环

```bash
curl -s -X POST http://127.0.0.1:8080/v1/spans \
  -H 'Content-Type: application/json' \
  -d @05-parent-cycle.json

curl -s http://127.0.0.1:8080/v1/traces/demo-cycle | jq '.latestRevision | {hasCycles, cyclePath}'
# => hasCycles=true, cyclePath=["a","b","c","a"]（自环 d 同样在 forest 节点上标 inCycle）
```

## 7. 修订历史

```bash
curl -s http://127.0.0.1:8080/v1/traces/demo-ooo/revisions | jq .
curl -s http://127.0.0.1:8080/v1/traces/demo-ooo/revisions/4 | jq .
```

## revision.reason 取值

| reason | 触发条件 |
|---|---|
| `initial` | trace 的第一个 span |
| `extended` | 新 span 挂载，trace 仍不完整 |
| `completed` | 本次 span 使之前不完整的 trace 变完整 |
| `conflict` | 同 spanId 冲突重发（首条为准，集合不变） |
| `timeout` | 缺根/不完整超时被密封 |
| `late` | 密封后又有 span 到达（可能直接补全） |
