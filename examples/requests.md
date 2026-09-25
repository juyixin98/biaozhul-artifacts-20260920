# HTTP 请求样例

以下样例假设服务运行在 `http://127.0.0.1:8080`（用 `./bin/wsd -addr ...` 启动）。
也可以直接运行 `../scripts/acceptance.sh`，它会启动服务并执行下面的大部分请求。

## 1. 健康检查与统计

```bash
curl -s http://127.0.0.1:8080/healthz
# {"ok":true,"outstanding":0,"workers":4}

curl -s http://127.0.0.1:8080/stats
```

## 2. 提交一个深递归任务（单线程饥饿死锁探针）

```bash
curl -s -X POST http://127.0.0.1:8080/tasks \
  -H 'Content-Type: application/json' \
  -d '{"kind":"recurse","payload":{"depth":6}}'
# {"ok":true,"task":{"id":"t-1","kind":"recurse","state":"pending",...}}
```

depth=6 的二叉树共 `2^7-1 = 127` 个节点，全部成功后 value=127。

## 3. 查询任务结果

```bash
curl -s http://127.0.0.1:8080/tasks/t-1
# {"ok":true,"task":{"id":"t-1","kind":"recurse","state":"succeeded",
#  "worker":1,"value":127,"attempts":1,...}}
```

## 4. 扇出 / 链 / 睡眠 / 失败 / panic / 空操作

```bash
# 扇出 8 个 noop
curl -s -X POST http://127.0.0.1:8080/tasks -H 'Content-Type: application/json' \
  -d '{"kind":"fanout","payload":{"count":8,"child_kind":"noop"}}'

# 100 级顺序链
curl -s -X POST http://127.0.0.1:8080/tasks -H 'Content-Type: application/json' \
  -d '{"kind":"chain","payload":{"count":100}}'

# 睡眠 250ms（时钟感知）
curl -s -X POST http://127.0.0.1:8080/tasks -H 'Content-Type: application/json' \
  -d '{"kind":"sleep","payload":{"millis":250}}'

# 显式失败
curl -s -X POST http://127.0.0.1:8080/tasks -H 'Content-Type: application/json' \
  -d '{"kind":"fail","payload":{"message":"something went wrong"}}'

# panic（被隔离，任务状态为 panicked，服务与 worker 不受影响）
curl -s -X POST http://127.0.0.1:8080/tasks -H 'Content-Type: application/json' \
  -d '{"kind":"panic","payload":{"message":"boom"}}'

# 空操作
curl -s -X POST http://127.0.0.1:8080/tasks -H 'Content-Type: application/json' \
  -d '{"kind":"noop"}'
```

## 5. 提交一个大任务并立即取消（随机取消的手动版）

```bash
# depth 很大，任务会运行一段时间，便于取消
RESP=$(curl -s -X POST http://127.0.0.1:8080/tasks -H 'Content-Type: application/json' \
  -d '{"kind":"recurse","payload":{"depth":14}}')
ID=$(echo "$RESP" | python3 -c 'import json,sys;print(json.load(sys.stdin)["task"]["id"])')

curl -s -X POST http://127.0.0.1:8080/tasks/$ID/cancel
# {"ok":true,"canceled":true,"task":{...,"state":"running"}}
#   （running 表示取消请求下达时该任务正在运行；随后变为 canceled，
#     仍在排队的后代直接变为 canceled 且永不执行）

curl -s http://127.0.0.1:8080/tasks/$ID
# {"ok":true,"task":{"id":"...","state":"canceled","err":"context canceled",...}}
```

对已结束任务再次取消返回 `"canceled":false`；对未知 id 返回 `404`。

## 6. 轮询等待任务结束（shell 小片段）

```bash
ID=t-1
while :; do
  ST=$(curl -s http://127.0.0.1:8080/tasks/$ID | python3 -c 'import json,sys;print(json.load(sys.stdin)["task"]["state"])')
  case "$ST" in
    succeeded|failed|panicked|canceled) echo "terminal: $ST"; break;;
  esac
  sleep 0.05
done
```

## 7. 读取结构化事件

```bash
curl -s 'http://127.0.0.1:8080/events'
# {"ok":true,"last_seq":123,"events":[...]}

# 增量拉取：只取 seq > 40 的事件
curl -s 'http://127.0.0.1:8080/events?after_seq=40'
```

事件字段：`seq`（全局单调递增）、`time`、`type`、`task_id`、`parent_id`、
`kind`、`state`、`worker`、`from`（窃取来源 worker）、`err`。

启动时加 `-event-log events.jsonl` 还会把同样的事件以 **JSON Lines** 落盘：

```bash
tail -f events.jsonl
```

## 8. 列出任务

```bash
curl -s 'http://127.0.0.1:8080/tasks?limit=50'
```

## 9. 错误处理

```bash
# 未知任务类型 -> 404
curl -s -i -X POST http://127.0.0.1:8080/tasks -H 'Content-Type: application/json' \
  -d '{"kind":"does-not-exist"}'

# 非法 JSON -> 400
curl -s -i -X POST http://127.0.0.1:8080/tasks -H 'Content-Type: application/json' \
  -d '{bad json'
```

## 10. 关闭

```bash
curl -s -X POST http://127.0.0.1:8080/shutdown
# {"ok":true,"status":"draining"}
# 之后新提交返回 503；排队任务取消，运行任务跑完后退出。
```

或者直接向进程发 `SIGTERM`/`SIGINT`（等价于带超时的优雅关闭）。
