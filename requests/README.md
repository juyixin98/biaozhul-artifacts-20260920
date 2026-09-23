# HTTP 请求样例

先启动服务：

```bash
go run ./cmd/chsim -listen 127.0.0.1:8080
```

## 1. 健康检查

```bash
curl -s http://127.0.0.1:8080/healthz
# ok
```

## 2. 扩容场景（并发写 + 迁移中加入新节点）

```bash
curl -s -X POST \
  -H 'Content-Type: application/json' \
  --data-binary @examples/scale_out.json \
  http://127.0.0.1:8080/run | jq '.verification, .stats.barriers'
```

## 3. 节点安全移除

```bash
curl -s -X POST \
  --data-binary @examples/remove_node.json \
  http://127.0.0.1:8080/run | jq '.verification.pass, .stats.nodes_decommissioned'
```

## 4. 迁移中断（暂停/恢复）+ 持续读写

```bash
curl -s -X POST \
  --data-binary @examples/interrupt.json \
  http://127.0.0.1:8080/run | jq '.verification, .stats.barriers[0].time_ms'
```

## 5. 混沌网络（25% 丢包 / 12% 重复 / 大抖动）+ 扩容后移除

```bash
curl -s -X POST \
  --data-binary @examples/chaos.json \
  http://127.0.0.1:8080/run \
  | jq '{pass: .verification.pass, dropped: .stats.messages_dropped,
          duplicated: .stats.messages_duplicated, retries: .stats.retries,
          barriers: .stats.barriers}'
```

## 6. 错误输入返回 400

```bash
curl -s -i -X POST -d '{bad' http://127.0.0.1:8080/run
# HTTP/1.1 400 Bad Request
# {"error":"invalid scenario JSON: ..."}
```

## 内联最小场景

```bash
curl -s -X POST -H 'Content-Type: application/json' \
  http://127.0.0.1:8080/run -d '{
    "seed": 1,
    "vnodes": 64,
    "nodes": [{"id":"n1"},{"id":"n2"},{"id":"n3"}],
    "network": {"base_delay_ms": 2, "jitter_ms": 3, "drop_rate": 0.05},
    "transfer": {"batch_size": 16, "concurrency": 2, "fetch_timeout_ms": 60},
    "clients": [{"id":"c1","start_ms":0,"ops":60,"interval_ms":4,"keys":20,"read_every":5}],
    "ops": [{"t":150,"op":"add_node","node_id":"n4"}]
  }' | jq '.verification'
```
