# HTTP 请求样例

以下样例假设服务运行在 `http://127.0.0.1:8080`（`./scripts/demo.sh` 会自动选空闲端口并把完整请求/响应记录到 `docs/demo-output.log`）。

完整自动化剧本：`./scripts/demo.sh`。以下为手工逐个执行的样例。

## 0. 健康检查

```bash
curl -s http://127.0.0.1:8080/healthz
# {"status":"ok"}
```

## 1. 注册节点（标签 + 容量）

```bash
curl -s -X POST http://127.0.0.1:8080/v1/nodes \
  -H 'Content-Type: application/json' \
  -d '{"name":"n1","capacity":6,"labels":{"zone":"a","tier":"cpu"}}'

curl -s -X POST http://127.0.0.1:8080/v1/nodes \
  -H 'Content-Type: application/json' \
  -d '{"name":"n2","capacity":6,"labels":{"zone":"b","tier":"cpu"}}'
```

## 2. 提交 gang（全组同时可放置 → HELD，返回预留计划与节点版本）

```bash
curl -s -X POST http://127.0.0.1:8080/v1/gangs \
  -H 'Content-Type: application/json' \
  -d '{
    "id":"A",
    "ttl_ms":5000,
    "tasks":[
      {"id":"a1","slots":4,"match_labels":{"zone":"a"}},
      {"id":"a2","slots":4,"match_labels":{"zone":"b"}}
    ]
  }'
```

响应中 `status:"HELD"`，`reservation.assignments` 给出每个任务落在哪个节点，
`reservation.node_versions` 是提交计划时必须回传的版本基线。

## 3. 提交竞争组（容量不足 → WAITING，不预留任何节点）

```bash
curl -s -X POST http://127.0.0.1:8080/v1/gangs \
  -H 'Content-Type: application/json' \
  -d '{
    "id":"B","ttl_ms":5000,
    "tasks":[
      {"id":"b1","slots":4,"match_labels":{"zone":"a"}},
      {"id":"b2","slots":4,"match_labels":{"zone":"b"}}
    ]
  }'
# status:"WAITING"，无 reservation 字段
```

## 4. 预留与提交之间下线节点（版本检查的目标场景）

```bash
curl -s -X POST http://127.0.0.1:8080/v1/nodes/n1/status \
  -H 'Content-Type: application/json' -d '{"status":"OFFLINE"}'
```

## 5. 用旧版本提交 → 409 VERSION_MISMATCH，全组 FAILED，无部分启动

```bash
curl -s -i -X POST http://127.0.0.1:8080/v1/gangs/A/commit \
  -H 'Content-Type: application/json' \
  -d '{"expected_node_versions":{"n1":1,"n2":1}}'
# HTTP 409
# {"error":{"code":"VERSION_MISMATCH","reason":"stale plan: node n1 changed"}}
```

## 6. 标签选择器：等值匹配 + IN 集合 + 反亲和（distinct_nodes）

```bash
curl -s -X POST http://127.0.0.1:8080/v1/nodes \
  -H 'Content-Type: application/json' \
  -d '{"name":"gpu-1","capacity":8,"labels":{"tier":"gpu","zone":"a"}}'
curl -s -X POST http://127.0.0.1:8080/v1/nodes \
  -H 'Content-Type: application/json' \
  -d '{"name":"gpu-2","capacity":8,"labels":{"tier":"gpu","zone":"b"}}'

curl -s -X POST http://127.0.0.1:8080/v1/gangs \
  -H 'Content-Type: application/json' \
  -d '{
    "id":"G","distinct_nodes":true,
    "tasks":[
      {"id":"t1","slots":1,"match_labels":{"tier":"gpu"}},
      {"id":"t2","slots":1,"in_labels":{"zone":["a","b"]}}
    ]
  }'
# distinct_nodes=true 时同组任务必须落在不同节点
```

## 7. 节点恢复 / 重新计划 / 提交

```bash
curl -s -X POST http://127.0.0.1:8080/v1/nodes/n1/status \
  -H 'Content-Type: application/json' -d '{"status":"ONLINE"}'

curl -s -X POST http://127.0.0.1:8080/v1/gangs/A/replan
# FIFO：队首先提升。拿到 HELD 后用响应里的新版本提交：
curl -s -X POST http://127.0.0.1:8080/v1/gangs/A/commit \
  -H 'Content-Type: application/json' \
  -d '{"expected_node_versions":{"n1":3,"n2":1}}'
```

## 8. 释放 / 删除 / 查询

```bash
curl -s -X POST http://127.0.0.1:8080/v1/gangs/A/release    # RUNNING 结束，槽位归还
curl -s -X DELETE http://127.0.0.1:8080/v1/gangs/A           # 需先 release
curl -s http://127.0.0.1:8080/v1/gangs                        # 列出所有 gang
curl -s http://127.0.0.1:8080/v1/nodes                       # 列出所有节点
curl -s http://127.0.0.1:8080/v1/state                       # 全量快照（调试/核对账目）
```

## 9. 预留超时（不提交自动过期，槽位释放，后继者提升）

```bash
curl -s -X POST http://127.0.0.1:8080/v1/gangs \
  -H 'Content-Type: application/json' \
  -d '{"id":"C","ttl_ms":300,"tasks":[{"id":"c1","slots":1}]}'
# 超过 300ms 未 commit，后台 reaper 将其置为
# FAILED: RESERVATION_EXPIRED 并释放全部预留槽位
```

## 错误响应格式

```json
{"error":{"code":"VERSION_MISMATCH","reason":"stale plan: node n1 changed"}}
```

| HTTP | code | 含义 |
| --- | --- | --- |
| 400 | `BAD_REQUEST` | JSON 非法 / 参数校验失败（空 id、slots≤0、版本未覆盖计划节点等） |
| 404 | `NOT_FOUND` | 节点或 gang 不存在 |
| 409 | `ALREADY_EXISTS` | 名字冲突 |
| 409 | `STATE` | 当前状态不允许该操作（如对 RUNNING 的 gang 做 replan） |
| 409 | `UNSATISFIABLE` | 提交复核时计划已不可行（节点不在线/容量不足/反亲和破坏） |
| 409 | `VERSION_MISMATCH` | 计划基于的节点版本已过期（预留后节点下线/替换） |
| 409 | `RESERVATION_EXPIRED` | 预留超时 |
