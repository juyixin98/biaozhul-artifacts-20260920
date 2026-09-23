# JSON-RPC 批处理网关（jsonrpc-gateway）

基于 Go 标准库 `net/http` 实现的本地 JSON-RPC 2.0 网关。纯后端，无界面。

## 功能

- **单条请求 / 通知 / 批处理**：完整支持 JSON-RPC 2.0 三种调用形态。
- **并发上限**：批处理内各项并发执行，全局信号量限制同时在跑的方法调用数（默认 8，可用 `-max-concurrency` 调整）。
- **ID 类型保留**：ID 以原始 JSON 字节保存和回显，字符串 `"1"` 与数字 `1` 严格区分；合法 ID 类型为字符串、数字、null。
- **错误码区分**：
  - `-32700` Parse error：请求体不是合法 JSON；
  - `-32600` Invalid Request：合法 JSON 但不是合法请求对象（缺 `jsonrpc`/`method`、版本错误、ID 类型非法、params 非数组/对象等）；
  - `-32601` Method not found、`-32602` Invalid params、`-32000` 服务端错误。
- **通知不产生响应**：单条通知或全通知批次返回 `HTTP 204` 空响应体；混合批次中通知项不出现在响应数组里。
- **响应顺序不保证**：批处理按完成顺序收集响应（并发执行，乱序完成），客户端必须按 `id` 匹配响应。
- 请求体上限 4 MiB。

## 依赖与构建

- Go ≥ 1.23（开发验证环境为 go1.23.4 linux/amd64）
- 仅使用标准库，无第三方依赖；`go.mod` 即锁定构建信息，无 `go.sum`。

```bash
go build -o jsonrpc-gateway .
```

## 启动

```bash
./jsonrpc-gateway -addr :8080 -max-concurrency 8
# 或不编译直接运行：
go run . -addr :8080
```

端点：

- `POST /rpc` — JSON-RPC 入口
- `GET /healthz` — 健康检查

## 内置示例方法

| 方法   | 参数                  | 说明                         |
| ------ | --------------------- | ---------------------------- |
| `echo` | 任意数组/对象         | 原样返回 params              |
| `add`  | 数字数组，如 `[1,2]`  | 求和                         |
| `fail` | —                     | 总是返回 `-32000` 错误       |
| `slow` | `{"ms": 300}`         | 睡眠指定毫秒后返回，演示并发 |

## 请求样例

单条请求：

```bash
curl -X POST localhost:8080/rpc \
  -d '{"jsonrpc":"2.0","method":"add","params":[1,2,3],"id":1}'
# {"jsonrpc":"2.0","result":6,"id":1}
```

通知（HTTP 204，无响应体）：

```bash
curl -X POST localhost:8080/rpc \
  -d '{"jsonrpc":"2.0","method":"echo","params":[1]}'
```

混合批次（含通知、非法项、未知方法，乱序完成）：

```bash
curl -X POST localhost:8080/rpc -d '[
  {"jsonrpc":"2.0","method":"slow","params":{"ms":500},"id":"slow-1"},
  {"jsonrpc":"2.0","method":"add","params":[10,20],"id":2},
  {"jsonrpc":"2.0","method":"echo","params":["notify-only"]},
  {"jsonrpc":"2.0","method":"no-such","id":"x"},
  {"broken":true},
  99,
  {"jsonrpc":"2.0","method":"echo","params":["fast"],"id":"fast-1"}
]'
```

实际返回（注意顺序按完成先后，通知项无响应，两个非法项各产生一条 `id:null` 的 `-32600`）：

```json
[
  {"jsonrpc":"2.0","result":["fast"],"id":"fast-1"},
  {"jsonrpc":"2.0","error":{"code":-32601,"message":"Method not found"},"id":"x"},
  {"jsonrpc":"2.0","error":{"code":-32600,"message":"Invalid Request"},"id":null},
  {"jsonrpc":"2.0","error":{"code":-32600,"message":"Invalid Request"},"id":null},
  {"jsonrpc":"2.0","result":30,"id":2},
  {"jsonrpc":"2.0","result":{"slept_ms":500},"id":"slow-1"}
]
```

解析错误 vs 无效请求：

```bash
curl -X POST localhost:8080/rpc -d '{"jsonrpc":"2.0","method":'
# {"jsonrpc":"2.0","error":{"code":-32700,"message":"Parse error"},"id":null}

curl -X POST localhost:8080/rpc -d '{"foo":"bar","id":7}'
# {"jsonrpc":"2.0","error":{"code":-32600,"message":"Invalid Request"},"id":7}

curl -X POST localhost:8080/rpc -d '[]'
# {"jsonrpc":"2.0","error":{"code":-32600,"message":"Invalid Request"},"id":null}
```

## 测试

```bash
go test -race -v ./...
```

覆盖场景（13 个用例，全部通过）：

- 单条请求的数字 / 字符串 ID 保留，且 `"1"` 与 `1` 可区分；
- 解析错误 `-32700` 与无效请求 `-32600` 的区分；
- 空数组返回单个 `-32600` 错误对象（非数组）；
- 全通知批次 / 单条通知 / 通知中方法不存在 → HTTP 204 空响应；
- 混合合法非法批次：响应数 = 输入数 − 通知数，按 ID 映射校验结果与错误码；
- 乱序完成：慢调用先提交、快调用后提交，快调用先出现在响应中，ID→结果映射仍正确；
- 并发上限：插桩方法实测同时在跑数不超过 `-max-concurrency`，且确实并行。

## 实测记录

- `go vet ./...`：通过。
- `gofmt -l .`：无输出（格式干净）。
- `go test -race -v ./...`：13/13 通过（go1.23.4 linux/amd64，含竞态检测）。
- 构建并启动 `./jsonrpc-gateway -addr :18080 -max-concurrency 4`，用 curl 实测上述全部样例，返回与本文档一致。

## 已知限制 / 未完成项

- 数字 ID 接受任意合法 JSON 数字（含 `1.0`、`1e3`），规范建议整数但未强制拒绝。
- 并发上限是进程内全局信号量，无每客户端/每方法维度的限流。
- 无认证、无 TLS（本地网关定位，如需暴露请置于反向代理之后）。
- 批量大小无单独上限（仅受 4 MiB 请求体限制）。
