# 示例请求（curl）

先启动：`npm start`（默认 http://127.0.0.1:8545）。所有示例用本地服务，无外部依赖。

## 单请求

```bash
curl -s -X POST http://127.0.0.1:8545 \
  -H 'content-type: application/json' \
  -d '{"jsonrpc":"2.0","method":"ping","id":1}'
# {"jsonrpc":"2.0","result":"pong","id":1}
```

## 严格参数校验：整数溢出 / 布尔不能当数字

```bash
curl -s -X POST http://127.0.0.1:8545 -H 'content-type: application/json' \
  -d '{"jsonrpc":"2.0","method":"calc.add","params":{"a":9007199254740991,"b":1},"id":2}'
# error.code = -32602（不会悄悄算成 9007199254740992）

curl -s -X POST http://127.0.0.1:8545 -H 'content-type: application/json' \
  -d '{"jsonrpc":"2.0","method":"calc.add","params":{"a":true,"b":2},"id":3}'
# error.code = -32602
```

## 批次：乱序完成、混合通知、非法元素

```bash
curl -s -X POST http://127.0.0.1:8545 -H 'content-type: application/json' \
  -d '[
    {"jsonrpc":"2.0","method":"tasks.start","params":{"delayMs":50,"label":"slow"},"id":"a"},
    {"jsonrpc":"2.0","method":"kv.put","params":{"key":"n1","value":1}},
    {"jsonrpc":"2.0","method":"ping","id":"b"},
    42,
    {"jsonrpc":"2.0","method":"echo","params":{"v":"x"},"id":null}
  ]'
# 数组长度 4：请求 a、b、id:null 各一个响应 + 非法元素一个 -32600；
# 通知 n1 不出现；顺序与输入一致，慢任务排第一。
```

## 空批次 vs 全通知批次

```bash
curl -s -o /dev/null -w '%{http_code}\n' -X POST http://127.0.0.1:8545 \
  -H 'content-type: application/json' -d '[]'                 # 200，体为单个 -32600

curl -s -o /dev/null -w '%{http_code}\n' -X POST http://127.0.0.1:8545 \
  -H 'content-type: application/json' \
  -d '[{"jsonrpc":"2.0","method":"ping"}]'                    # 204，空体
```

## 批次内重复 ID

```bash
curl -s -X POST http://127.0.0.1:8545 -H 'content-type: application/json' \
  -d '[
    {"jsonrpc":"2.0","method":"echo","params":{"v":1},"id":7},
    {"jsonrpc":"2.0","method":"echo","params":{"v":2},"id":7}
  ]'
# 第一个正常 result；第二个 error.code = -32001，两者 id 都是 7，位置不串。
```

## 解析错误分层

```bash
curl -s -X POST http://127.0.0.1:8545 -H 'content-type: application/json' -d 'garbage{'
# error.code = -32700，id=null（HTTP 仍是 200，错误在 JSON-RPC 层表达）
```

## 副作用、独立操作号与幂等

```bash
# 首次写入
curl -s -X POST http://127.0.0.1:8545 -H 'content-type: application/json' \
  -d '{"jsonrpc":"2.0","method":"kv.put","params":{"key":"k","value":"v1"},"id":1}'
# result.operationId = op_...（与 RPC id 无关）

# 同键同参数重试：回放，不产生第二次写入
curl -s -X POST http://127.0.0.1:8545 -H 'content-type: application/json' \
  -d '{"jsonrpc":"2.0","method":"kv.put","params":{"key":"k","value":"v1"},"id":2}'

# 同键不同参数：冲突 -32005
curl -s -X POST http://127.0.0.1:8545 -H 'content-type: application/json' \
  -d '{"jsonrpc":"2.0","method":"kv.put","params":{"key":"k","value":"v2"},"id":3}'
```

## 异步任务：启动 → 查询 → 取消

```bash
# 启动
curl -s -X POST http://127.0.0.1:8545 -H 'content-type: application/json' \
  -d '{"jsonrpc":"2.0","method":"tasks.start","params":{"delayMs":2000,"label":"job"},"id":1}'
# 记下 result.operationId

# 查询（替换 OPID）
curl -s -X POST http://127.0.0.1:8545 -H 'content-type: application/json' \
  -d '{"jsonrpc":"2.0","method":"operations.get","params":{"operationId":"OPID"},"id":2}'

# 取消
curl -s -X POST http://127.0.0.1:8545 -H 'content-type: application/json' \
  -d '{"jsonrpc":"2.0","method":"tasks.cancel","params":{"targetOperationId":"OPID","reason":"demo"},"id":3}'
# 之后 operations.get 显示 status=cancelled, errorCode=-32007
```

## 查询操作记录与诊断事件

```bash
curl -s -X POST http://127.0.0.1:8545 -H 'content-type: application/json' \
  -d '{"jsonrpc":"2.0","method":"operations.list","params":{"status":"failed","limit":20},"id":4}'

curl -s -X POST http://127.0.0.1:8545 -H 'content-type: application/json' \
  -d '{"jsonrpc":"2.0","method":"diagnostics.events","params":{"limit":50},"id":5}'
```

## 脱敏：密钥不入库、不入日志

```bash
curl -s -X POST http://127.0.0.1:8545 -H 'content-type: application/json' \
  -d '{"jsonrpc":"2.0","method":"kv.put","params":{"key":"cred","value":{"password":"hunter2"}},"id":6}'
curl -s -X POST http://127.0.0.1:8545 -H 'content-type: application/json' \
  -d '{"jsonrpc":"2.0","method":"operations.list","params":{"method":"kv.put"},"id":7}'
# 再用 operations.get 查看 request.value.password，值为 ***REDACTED***。
```
