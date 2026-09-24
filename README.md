# JSON-RPC 2.0 批处理网关（Go / net/http）

纯后端的 JSON-RPC 2.0 本地网关：单请求、通知、批处理、并发上限；严格区分
**解析错误（-32700）** 与 **无效请求（-32600）**；字符串 ID 与数字 ID 原样
保留（大整数不被 float64 舍入）；通知不产生响应；批处理各项并发执行，响应
**按完成顺序**返回，客户端通过 `id` 字段匹配（响应顺序不保证等于输入顺序）。

## 依赖与环境

- Go 1.23+（开发环境实测 go1.23.4 linux/amd64）
- **零第三方依赖**：仅使用 Go 标准库（`net/http`、`encoding/json`、`sync/atomic` 等）
- 因此：
  - `go.mod` 即依赖清单，无 `require` 块；
  - 不需要 `go.sum`（没有任何需要校验和的外部模块）；
  - Go 工具链版本本身由 `go.mod` 的 `go 1.23` 指令锁定；标准库随工具链发布。
  - 若希望连构建工具链也可复现，可另用 `go1.23.4` 固定的工具链名构建或容器化部署。

## 启动命令

```bash
go run .                          # 默认监听 :8080，并发上限 64
# 或
go build -o jsonrpc-gateway .
./jsonrpc-gateway -addr :8080 -concurrency 8 -max-body 10485760
```

参数（也可用环境变量 `JSONRPC_ADDR` / `JSONRPC_CONCURRENCY` / `JSONRPC_MAX_BODY`）：

| 参数 | 默认 | 说明 |
|---|---|---|
| `-addr` | `:8080` | 监听地址 |
| `-concurrency` | `64` | 同时执行的方法处理器上限（所有 HTTP 连接共享），`0` 为不限 |
| `-max-body` | `10485760`（10 MiB） | 请求体大小上限，超出返回 HTTP 413 |

## HTTP 接口

- 路径：任意路径均指向同一处理器（示例统一用 `POST /`）
- 方法：仅 `POST`，其他方法返回 `405 Method Not Allowed`（带 `Allow: POST`）
- 请求体：一个 JSON-RPC 对象，或一个 JSON-RPC 对象数组
- HTTP 状态约定：
  - `200`：有 JSON 响应（含各类 JSON-RPC 错误）
  - `204 No Content`：单条通知，或批次内全部是通知
  - `405`：非 POST；`413`：请求体过大

### 内置方法

| 方法 | 参数 | 返回 |
|---|---|---|
| `ping` | 无 | `"pong"` |
| `echo` | 任意 | 原样回显 params（数字保持精确） |
| `sum` | 数字数组（位置参数） | 合计；全整数时返回整数，否则返回浮点 |
| `subtract` | `[a,b]` 或 `{"a":n,"b":n}` | a−b，整数结果保持整数 |
| `delay` | `[毫秒数]`（默认 50ms，上限 30s） | 等待后返回 `"done"`，用于演示并发与乱序 |

### 错误码

| code | 含义 | 触发场景 |
|---|---|---|
| -32700 | Parse error | 请求体不是合法 JSON（空体、语法损坏、值后有多余 token） |
| -32600 | Invalid Request | JSON 合法但不符合协议（非对象/数组、缺少 jsonrpc/method、版本不为 "2.0"、method 非字符串、params 非数组或对象、id 类型非法、空数组批次） |
| -32601 | Method not found | 方法未注册 |
| -32602 | Invalid params | 请求结构合法，但参数内容不合法 |
| -32603 | Internal error | 处理器返回了非协议错误（内部细节不外泄） |

协议要点：

- **ID 保留规则**：`id` 按原始 JSON 字节回显。数字 ID 用 `json.Number`
  解码，`1` 与 `"1"` 严格区分，`9007199254740993` 等超出 2^53 的整数不丢精度。
- **无效项的 id 一律为 `null`**：项本身不是对象，或对象不符合协议时，
  服务端无法信任其中的 id，按规范返回 `id: null`。
- **通知判定**：没有 `id` 字段即为通知（`"id":null` 是显式 null 的普通请求，
  必须照常响应）；通知即使方法不存在也不响应。
- **空数组 `[]`**：视为一个无效请求，返回单个 `-32600` 错误对象（不是 `[]`）。
- **批处理并发**：各项用 goroutine 并发执行，受同一个信号量闸门约束；
  响应数组按完成先后收集，顺序不保证与输入一致，用 id 匹配。

## 请求样例

完整可执行样例：`examples/curl-examples.sh`（`BASE=http://127.0.0.1:8080 bash examples/curl-examples.sh`）。

```bash
# 单请求（命名参数 + 数字 ID）
curl -s -X POST http://127.0.0.1:8080 -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","method":"subtract","params":{"a":42,"b":17},"id":2}'
# -> {"jsonrpc":"2.0","result":25,"id":2}

# 通知 -> HTTP 204，无响应体
curl -i -X POST http://127.0.0.1:8080 -d '{"jsonrpc":"2.0","method":"ping"}'

# 混合批次（请求 + 通知 + 非法项）
curl -s -X POST http://127.0.0.1:8080 -d '[
  {"jsonrpc":"2.0","method":"sum","params":[1,2,3],"id":1},
  {"jsonrpc":"2.0","method":"ping"},
  {"jsonrpc":"2.0","method":"nope","id":"x"},
  {"foo":"bar"},
  {"jsonrpc":"2.0","method":"ping","id":"y"}
]'

# 乱序完成（A=300ms, B=100ms, C=200ms，实际返回 B,C,A）
curl -s -X POST http://127.0.0.1:8080 -d '[
  {"jsonrpc":"2.0","method":"delay","params":[300],"id":"A"},
  {"jsonrpc":"2.0","method":"delay","params":[100],"id":"B"},
  {"jsonrpc":"2.0","method":"delay","params":[200],"id":"C"}
]'
```

## 自动化测试

```bash
go test -race -v ./...        # 竞态检测 + 详细输出
go test -race -count=3 ./...  # 重复运行排查时序类 flaky
gofmt -l . && go vet ./...    # 格式与静态检查（应无输出/通过）
```

测试覆盖（`server_test.go`）：

- 字符串 ID、数字 ID（含 >2^53 大整数）原样回显；`1` 与 `"1"` 不混淆
- 单通知、全通知批次 → 204 无体
- 空数组 → 单个 -32600
- 混合批次：合法请求成功、通知无响应、未知方法 -32601、参数错误 -32602、
  非对象/`null`/`42` 各得 -32600 且 id 为 null，并逐条校验 id 映射
- 乱序完成：三个 delay 实际按 B,C,A 完成，且耗时证明并发执行
- 并发上限：40 个请求、上限 4 时原子计数观测峰值 ≤4 且确实并发，40 个 id 全部有对应响应
- -32700 vs -32600：空体、损坏 JSON、尾随 token 为 -32700；标量、缺字段、
  错误版本、`method:null`、params/id 类型错误为 -32600
- 显式 `id:null` 是请求不是通知；GET → 405；命名/位置参数；数字回显精度

### 实测结果（2026-09-24，本机 go1.23.4）

```
$ go vet ./... && go build -o jsonrpc-gateway .   # 通过，无输出
$ go test -race -count=3 ./...
ok  	jsonrpc-gateway	2.610s
```

示例脚本对运行中的服务实测关键输出：

```
混合批次（5 项含 1 通知）：
[{"result":"pong","id":"y"},{"result":6,"id":1},
 {"error":{"code":-32600,...},"id":null},
 {"error":{"code":-32601,...},"id":"x"}]

乱序批次：[{"result":"done","id":"B"},{"result":"done","id":"C"},{"result":"done","id":"A"}]
大整数 ID：{"result":"pong","id":9007199254740993}
空数组：  {"error":{"code":-32600,"message":"Invalid Request",...},"id":null}
损坏 JSON：{"error":{"code":-32700,"message":"Parse error"},"id":null}
通知：    HTTP/1.1 204 No Content（无 body）
GET /：   HTTP/1.1 405 Method Not Allowed, Allow: POST
```

## 文件结构

```
go.mod                  模块声明（无外部依赖，Go 版本锁定）
errors.go               RPC 错误类型与标准错误码
server.go               协议解析、批处理调度、并发闸门、HTTP Handler
methods.go              内置示例方法（ping/echo/sum/subtract/delay）
main.go                 启动入口（flag/环境变量配置、优雅关闭、访问日志）
server_test.go          验收测试（16 个用例，含并发与乱序）
examples/curl-examples.sh  可执行 curl 样例集
```

## 未完成项 / 已知边界

- 无鉴权、无 TLS：定位为本地网关，生产暴露需在前置反向代理补 HTTPS 与认证。
- 批处理没有整体截止时间参数；客户端断开会通过 request context 取消排队中的处理器。
- `sum` 整数结果为 int64，超出 int64 范围的输入按浮点处理；未引入 big 数运算。
- `delay` 仅用于演示/测试并发，默认开启；如需收紧可在 `registerBuiltins` 中移除。
