# respd — RESP2 流水线服务器（Go / net/http）

纯后端服务：通过 **HTTP** 承载 **RESP2**（Redis Serialization Protocol v2）键值命令，
支持二进制安全 bulk、数组、**流水线（pipelining）** 与 **MULTI/EXEC 事务排队**。
严格区分 NULL（`$-1`）与空字符串（`$0\r\n\r\n`），对嵌套深度、声明长度、
行长度做严格限制；协议解析器是**流式**的——可以一个字节一个字节喂数据，
帧没收完绝不会提前执行。

- 语言：Go（仅标准库，**零第三方依赖**）
- 模块：`respd`（Go 1.22+；本机验证 Go 1.23.4）

---

## 1. 目录结构

```
.
├── go.mod                        模块声明（无 require，即依赖已锁定：标准库）
├── main.go                       net/http 启动入口（flag、优雅关闭）
├── Makefile
├── README.md
├── internal/
│   ├── resp/                     RESP2 严格编解码器
│   │   ├── value.go              Value 类型、NULL/空串区分、ProtocolError
│   │   ├── decoder.go            流式解析器 + 长度/深度上限 + inline 支持
│   │   ├── encoder.go            生产编码器（CR/LF 注入防护）
│   │   └── resp_test.go          协议测试（独立编码器对照/逐字节/半包/负长度）
│   ├── kv/                       线程安全内存键值库
│   │   ├── store.go              16 个 DB、TTL 惰性过期、glob KEYS、INCR
│   │   └── store_test.go
│   └── server/                   HTTP + 命令表 + 事务
│       ├── server.go             会话、MULTI 排队、EXEC 原子执行
│       ├── commands.go           命令表（PING/GET/SET/INCR/… 共 26 条）
│       ├── http.go               POST /resp、POST /exec、/healthz、/
│       ├── logging.go            访问日志中间件
│       └── server_test.go        端到端测试（真实 TCP、逐字节、断连、事务）
└── examples/
    ├── acceptance.py             独立 Python 验收脚本（自带独立编码器/解码器）
    └── curl-examples.sh          curl 请求样例
```

---

## 2. 启动命令与依赖

### 依赖

- Go ≥ 1.22（仅用标准库；`go.mod` 无 `require`，`go.sum` 不需要存在）
- 验收脚本另外需要 Python ≥ 3.6（只用标准库）

无需联网下载任何模块。

### 启动

```bash
go run .                         # 默认监听 :7379
go run . -addr 127.0.0.1:8080    # 指定地址
go run . -max-body 1048576 -idle-ttl 10m
# 或
make build && ./bin/respd
# 环境变量 RESPD_ADDR 也可指定监听地址
```

启动后：

| 方法   | 路径       | Content-Type              | 说明 |
|--------|------------|---------------------------|------|
| `POST` | `/resp`    | `application/octet-stream`| 请求体是一条或多条 RESP2 命令数组；响应体是**按序排列的 RESP2 回复** |
| `POST` | `/exec`    | `application/json`        | 便捷 JSON 接口：`{"session":"s1","command":["SET","k","v"]}` |
| `GET`  | `/healthz` | —                         | 存活探针 |
| `GET`  | `/`        | —                         | 接口说明（JSON） |

可选请求头 **`X-Session: <id>`**（可打印 ASCII，≤128 字节）：
同一 id 的请求共享事务状态（MULTI 队列）与 `SELECT` 选定的 DB；
不带该头的请求使用一次性会话（但**单次请求体内仍可完成 MULTI…EXEC 流水线**）。
空闲会话 5 分钟后被后台清理。

### HTTP 状态码约定（`/resp`）

- `200`：所有帧完整，回复全部在体内（空请求体也是 200、空回复体）；
- `400`：请求体在某一帧中途截断或违反 RESP2 语法。响应体仍为 RESP2：
  **出错前已完成命令的回复照常返回**，最后追加一帧
  `-ERR Protocol error: …`，出错后的命令不再执行，当前事务（若有）丢弃；
- `413`：请求体超过 `-max-body`（默认 128 MiB）；
- `405`：对 `/resp`、`/exec` 使用非 POST。

---

## 3. RESP2 与限制

| 限制 | 默认值 | 说明 |
|------|--------|------|
| `MaxLineLen` | 64 KiB | simple/error/integer 行及各类头行 |
| `MaxBulkLen` | 64 MiB | bulk 声明长度，超限在读取负载前直接拒绝 |
| `MaxArrayLen` | 1,000,000 | 数组声明元素数 |
| `MaxNesting` | 7 | 数组最大嵌套深度（第 8 层拒绝） |
| `MaxInlineLen` | 64 KiB | inline 文本协议行 |
| HTTP body | 128 MiB | `-max-body` 可调 |

长度字段采用严格整数语法：可选单个 `-` 后必须是纯数字；
bulk/数组只接受非负数或恰好 `-1`（NULL），`-2` 等一律协议错误；
整数严格按 int64 范围（含 ±边界值）。

NULL 与空值的区别被完整保留：

```
$-1\r\n           -> NullBulk()：GET 不存在的键
$0\r\n\r\n        -> BulkBytes([]byte{})：GET 值为 "" 的键
*-1\r\n           -> NullArray()
*0\r\n            -> 空数组
```

### 支持的命令

`PING` `ECHO` `GET` `SET`（含 `EX/PX/NX/XX/KEEPTTL`）`GETSET` `MSET` `MGET`
`APPEND` `STRLEN` `INCR` `INCRBY` `DECR` `DECRBY` `EXISTS` `DEL` `TYPE`
`KEYS`（glob：`* ? [a-z] [^x] \x`）`DBSIZE` `FLUSHDB` `SELECT`（0–15）
`EXPIRE` `PEXPIRE` `TTL` `PTTL` `PERSIST` `COMMAND`

### 事务语义（对齐 Redis）

- `MULTI` 后命令返回 `+QUEUED` 并排队；
- **排队期错误**（未知命令、参数个数错误）立即回错误并置脏，`EXEC` 时整个事务
  返回 `EXECABORT`，**任何排队命令都不执行**；
- **运行期错误**（如对字符串 INCR）只在 EXEC 数组中对应位置产生错误回复，
  不影响其余命令；
- `DISCARD` 清空队列；`MULTI` 不可嵌套；`EXEC` 无 MULTI 时按 Redis 文案报错；
- 所有命令在一把全局串行锁下执行，EXEC 队列作为一个不可分割的批次运行。

---

## 4. 请求样例

最简单（服务在跑时）：

```bash
# 两条流水线命令；回复按顺序：+PONG 和 bulk "hello"
{ printf '*1\r\n$4\r\nPING\r\n'; printf '*2\r\n$4\r\nPING\r\n$5\r\nhello\r\n'; } \
  | curl -s --data-binary @- http://127.0.0.1:7379/resp

# SET 空串 -> GET -> GET 不存在的键
printf '*3\r\n$3\r\nSET\r\n$1\r\ne\r\n$0\r\n\r\n' \
  | curl -s --data-binary @- http://127.0.0.1:7379/resp
# 响应：+OK、$0（空串）、$-1（NULL）
```

更多样例（含二进制、事务、X-Session、JSON、半包）：

```bash
bash examples/curl-examples.sh
```

JSON 接口：

```bash
curl -s -H 'Content-Type: application/json' \
  -d '{"session":"j1","command":["MSET","k","v"]}' \
  http://127.0.0.1:7379/exec
# {"type":"simple","value":"OK"}
curl -s -H 'Content-Type: application/json' \
  -d '{"session":"j1","command":["GET","k"]}' \
  http://127.0.0.1:7379/exec
# {"type":"bulk","value":"v"}
# 非 UTF-8 的 bulk 以 {"type":"bulk","binary_b64":"…"} 返回
```

### 独立验收脚本（题目要求的四类验收点）

```bash
make acceptance                 # 自动 go run 起服务，跑完自动退出
python3 examples/acceptance.py 127.0.0.1:7379   # 或连接已运行实例
```

脚本用 Python **独立实现**了一套 RESP2 编码器和解码器（字符串拼接，不共享 Go 代码），
通过**裸 TCP 套接字**逐字节发送整个 HTTP 请求，覆盖：

1. 协议单字节发送（101 条 INCR 流水线 + 空值/空串/二进制负载）；
2. 负长度与非法帧（`$-2`、`*-3`、声明长度不符、非数字长度）；
3. 半包断连（Content-Length 虚报后立即 FIN）与请求体内半包（400 + 已完成回复 + 终止错误帧）；
4. 事务中语法错误（未知命令、参数个数错误 → EXECABORT 且无副作用）；
5. 独立编码器往返对照与会话、JSON 接口冒烟。

---

## 5. 自动化测试

```bash
make test      # go test ./...
make race      # go test -race ./...
make vet       # go vet ./...
```

测试结构（56 个顶层/表驱动用例，含子测试）：

- `internal/resp/resp_test.go`
  - **独立参考编码器**（`independentEncoder`，纯字符串拼接）逐字节对照生产编码器；
  - `oneByteReader` / `slowReader` 两种慢喂法，含 2000 条命令流水线；
  - 负长度/非法整数/缺少 CRLF/错误类型字节等 10 种坏帧；
  - 声明长度超限（bulk/数组/行）、嵌套深度 7 通过/8 拒绝；
  - 对每一帧的**每个切点**截断，验证都返回 `Truncated()` 协议错误；
  - `$0` 与 `$-1` 语义断言；inline 命令解析。
- `internal/kv/store_test.go`：空串/缺失区分、二进制拷贝隔离、
  INCR 严格语法与溢出、DB 隔离、TTL 过期、glob 匹配。
- `internal/server/server_test.go`：真实 `httptest` TCP 监听器——
  逐字节 HTTP 发送、半包 + 断连后服务存活、负长度 400、
  MULTI/EXEC 成功与两类中止、运行期错误局部化、跨请求 X-Session、
  DB 选择隔离、413、JSON 接口、独立编码器线格式对照、16×50 并发（配合 `-race`）。

---

## 6. 实际运行结果（如实记录）

以下结果在开发机（Ubuntu 24.04，Go 1.23.4）实际执行：

- `go vet ./...`：无输出（通过）；
- `go test -timeout 60s ./...`：三个包全部 `ok`；
- `go test -race -timeout 120s ./...`：三个包全部 `ok`，无数据竞争；
- `python3 examples/acceptance.py`：见下方“验收脚本实跑记录”；
- 开发中发现并修复的真实问题（测试驱动抓出）：
  1. 整数帧最初漏发结尾 `CRLF`，导致流水线回复粘连——逐字节往返测试与独立
     编码器对照立即失败，已修复并固化在测试中；
  2. int64 边界判定把 `MaxInt64/MinInt64` 误判为溢出（负数上限应是 2^63），
     kv 的 INCR 解析存在同样问题，均已修复并加入边界样本；
  3. handler 与 dispatch 双重获取会话锁导致自死锁，测试整体挂起，
     拆成 `dispatch(lock=true)` / `dispatchLocked(lock=false)` 后通过。

---

## 7. 设计取舍与**未完成项 / 限制**

- **HTTP 语义 vs Redis TCP**：本服务不是 6379 裸 RESP 端口，而是把 RESP 帧装进
  HTTP 请求体。Redis TCP 模式下“半包”只需等待更多字节；HTTP 模式下请求体结束即
  断帧，因此半包被定义为 400 协议错误，这是题目要求与 HTTP 载体共同决定的语义。
  响应为缓冲后一次写出（带 `Content-Length`），没有做逐回复 `Flush` 的流式输出，
  超长流水线会占用与响应等大的内存（受 body 上限间接约束）。
- 数据结构只有 string 类型，故 `TYPE` 只可能返回 `string`/`none`；
  list/hash/set/zset/stream、`SUBSCRIBE`、Lua、ACL、持久化（RDB/AOF）均未实现。
- 会话是内存态，重启丢失；`X-Session` 是自报身份，没有认证（教学/内网用途）。
- TTL 仅惰性过期（访问时清理），没有后台主动过期扫描；`KEYS` 是全量扫描，
  未做大键数保护。
- inline 协议不支持引号转义（与 Redis 相同的空白分词）。
- `go.mod` 刻意声明 `go 1.22` 且无任何 `require`；本机存在两套 Go 1.23 工具链，
  其中 `~/sdk/go` 的 GOROOT 文件疑似损坏（runtime 文件重复声明），
  实跑使用的是 `~/.local/go`（1.23.4）。
