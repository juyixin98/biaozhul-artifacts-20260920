# respd — RESP2 流水线服务器（Go / net/http）

一个**纯后端**的 RESP2（REdis Serialization Protocol, v2）键值服务。
协议走 HTTP `POST /resp` 传输：请求体是一条或多条流水线 RESP 命令，
响应体是按顺序排列的 RESP 回复。仅依赖 Go 标准库，无任何第三方包。

## 特性

- **RESP2 五种类型**：simple string、error、integer、binary-safe bulk
  string、array，以及两种 null（`$-1` / `*-1`）。
- **空值 ≠ 空字符串**：
  - 不存在的键 → null bulk：`$-1\r\n`
  - 值为空串的键 → 长度 0 的 bulk：`$0\r\n\r\n`
- **二进制安全**：bulk 载荷可包含 `\r`、`\n`、`\x00`、任意高字节；解析以
  声明长度为准，从不扫描载荷。
- **流水线**：一个 HTTP 请求里可放任意多条命令，回复与命令一一对应。
- **事务排队**：`MULTI` / `EXEC` / `DISCARD`，入队即校验命令名与参数个数；
  事务中出现**语法类错误**（未知命令、参数个数错误）会置位 abort，
  `EXEC` 返回 `EXECABORT` 且整个事务不执行。执行期错误（如对非整数
  `INCR`）只在回复数组里对应位置报错，不影响其余命令。
- **严格限制**（防御畸形/恶意输入，声明值先校验后分配）：
  - 单个 bulk 最大 16 MiB（`MaxBulkLength`）
  - 单行头最大 64 KiB（`MaxInlineLine`）
  - 单数组元素数 ≤ 1 048 576（`MaxArrayElements`）
  - 数组嵌套深度 ≤ 7（`MaxNestingDepth`）
  - 整数字段必须规范：可选单个前导 `-`、无前导零、无 `-0`、无 `+`、无空格
  - 只允许 bulk 长度/数组数量为 `-1`（null），其它负数一律协议错误
  - HTTP 请求体上限 64 MiB（`http.MaxBytesReader`，超限返回 413）
- **半包识别**：流在一帧中间结束 → 协议层返回 `ErrUnexpectedEOF`；
  帧边界处干净结束 → `io.EOF`。HTTP 层把半包/协议错误作为流末尾的一条
  RESP error 返回，此前已完成的流水线命令回复不受影响。

## 目录结构

```
cmd/respd/        服务入口（net/http，优雅关闭）
cmd/respc/        示例客户端 / 请求体生成器
internal/resp/    严格 RESP2 编解码器（Reader/Writer/Value）
internal/kv/      内存键空间、命令表、MULTI 会话、glob 匹配
internal/server/  HTTP handler 与端到端测试
go.mod            模块文件（无第三方依赖，因此没有 go.sum）
```

## 依赖

- Go ≥ 1.23（开发与验证使用 go1.23.4 linux/amd64）
- 仅标准库：`net/http`、`bufio`、`encoding/json` 等；**零第三方依赖**。
  没有需要下载的模块，`go.sum` 因此不存在——`go.mod` 即为完整、可复现的
  依赖锁定文件。

## 启动

```bash
go run ./cmd/respd -addr :8080
# 或
go build -o respd ./cmd/respd && ./respd -addr :8080
```

可选参数：

- `-addr`：监听地址，默认 `:8080`（也可用环境变量 `RESPD_ADDR`）
- `-max-body`：单请求体字节上限，默认 67108864

启动后：

| 方法 & 路径     | 说明                                            |
| --------------- | ----------------------------------------------- |
| `POST /resp`    | 请求体为流水线 RESP2，响应体为流水线 RESP2 回复 |
| `GET /healthz`  | 固定返回 `ok`                                   |
| `GET /commands` | 命令表（JSON，含名称/arity/说明）               |

## 请求样例

一条 RESP 命令就是「bulk string 数组」。例如 `SET greet hello`：

```
*3\r\n$3\r\nSET\r\n$5\r\ngreet\r\n$5\r\nhello\r\n
```

### 1) 用示例客户端（推荐）

```bash
# 一次请求内流水线多条命令，"--" 分隔命令
go run ./cmd/respc -url http://127.0.0.1:8080/resp \
  PING -- SET greet "a b" -- GET greet -- GET missing -- MULTI -- INCR n -- INCR n -- EXEC

# 只打印请求体（便于拿去喂 curl 或做协议对照）
go run ./cmd/respc -encode PING -- SET k v
```

### 2) curl + 二进制安全请求体

用 `respc -encode` 生成原始请求体，`curl --data-binary` 原样发送：

```bash
go run ./cmd/respc -encode PING -- SET greet hello -- GET greet -- GET missing \
  > /tmp/req.bin
curl -sS --data-binary @/tmp/req.bin http://127.0.0.1:8080/resp | xxd
```

期望响应（可见末尾 `GET missing` 是 `$-1` 空值，而非 `$0` 空串）：

```
00000000: 2b50 4f4e 470d 0a2b 4f4b 0d0a 2435 0d0a  +PONG.+OK..$5..
00000010: 6865 6c6c 6f0d 0a24 2d31 0d0a            hello..$-1..
```

### 3) 纯 printf 手搓（不借助客户端）

```bash
# 单条 PING
printf '*1\r\n$4\r\nPING\r\n' | curl -sS --data-binary @- http://127.0.0.1:8080/resp
# -> +PONG

# 流水线：SET k v 后 GET k（载荷里故意放 CRLF/NUL 也安全）
{ printf '*3\r\n$3\r\nSET\r\n$1\r\nk\r\n$5\r\n'; printf 'a\r\n\000'; printf '\r\n';
  printf '*2\r\n$3\r\nGET\r\n$1\r\nk\r\n'; } \
  | curl -sS --data-binary @- http://127.0.0.1:8080/resp | xxd
```

健康检查与命令表：

```bash
curl -sS http://127.0.0.1:8080/healthz
curl -sS http://127.0.0.1:8080/commands | head -c 400; echo
```

## 支持的命令

| 命令                | arity*                          | 说明                                   |
| ------------------- | ------------------------------- | -------------------------------------- |
| `PING [msg]`        | ≥1                              | PONG / 原样 bulk 回显                  |
| `ECHO msg`          | 2                               | 回显                                   |
| `SET k v [NX\|XX]`  | ≥3                              | NX 仅不存在时、XX 仅存在时设置         |
| `GET k`             | 2                               | 值 bulk；不存在为 `$-1`                |
| `DEL k [k…]`        | ≥2                              | 删除计数                               |
| `INCR k`            | 2                               | 整数字符串自增                         |
| `APPEND k v`        | 3                               | 追加，返回新字节长度                   |
| `STRLEN k`          | 2                               | 字节长度                               |
| `TYPE k`            | 2                               | `string` / `none`                      |
| `KEYS pattern`      | 2                               | glob：`* ? [a-z] [^x] \x`，结果排序    |
| `MULTI/EXEC/DISCARD`| 1                               | 事务                                   |

\* arity 为 Redis 约定：正数=含命令名的精确参数数；负数 -N=至少 N 个。

## 事务语义（与 Redis 对齐）

```
-> MULTI          <- +OK
-> SET k 1        <- +QUEUED
-> BOGUS x        <- -ERR unknown command 'bogus'   （入队期语法错误，置 abort）
-> GET            <- -ERR wrong number of arguments（继续排队/继续报错）
-> EXEC           <- -EXECABORT Transaction discarded because of previous errors.
-> GET k          <- $-1                            （没有任何命令执行）
```

执行期错误不中止事务：`INCR` 一个非整数键只在结果数组对应位置产生
`-ERR`，后续命令照常执行（测试 `TestRuntimeErrorInsideExec` 覆盖）。

## 自动化测试

```bash
go test ./...            # 全部单测 + httptest 端到端
go test -race ./...      # 竞态检测
go test -fuzz=FuzzCodecRoundTrip -fuzztime=30s ./internal/resp
```

测试覆盖（对应验收点）：

- **逐字节发送**：`oneByteReader` 每次 `Read` 只返回 1 字节
  （协议层与 HTTP handler 各一个测试），含随机嵌套/二进制载荷。
- **对照独立编码器往返**：测试内有第二份独立编码器 `encodeRef`
  （`fmt.Fprintf` 手写），逐字节比对产品 `Writer` 输出，并双向往返。
- **负长度**：`$-2`、`*-2` 拒绝；`$-1`/`*-1` 作为 null 接受。
- **半包断连**：在一帧的 7 个不同截断点 EOF，均返回 `ErrUnexpectedEOF`；
  HTTP 下前面已完成的流水线命令回复保留，末尾追加一条 error 回复。
- **事务中语法错误**：未知命令 + 参数个数错误 → `EXECABORT`，存储不被修改。
- 空值/空串区分、深度上限、声明长度上限、整数规范化、413 超限、
  非数组帧不连累相邻流水线命令。
- Fuzz：任意字节流若被解析成功，规范化重编码后必须再次解析为相同值。

## 设计要点

- **解析与执行解耦**：`resp.Reader` 只产出 `*resp.Value` 树；`kv.Session`
  消费 `[]string` 命令向量，不知道任何 HTTP/字节细节。普通执行与事务执行
  共用 `Store.exec` 同一路径，语义不会分叉。
- **HTTP 无状态**：每个 `POST /resp` 新建一个 `Session`，`MULTI` 状态不会
  跨请求泄漏；事务边界就在单个流水线请求体内。
- **先校验后分配**：bulk/数组大小在读取载荷前全部检查，超大声明不会触发
  对应内存分配。

## 已知边界 / 未完成项

- 仅字符串类型与单库（无 hash/list/set/zset、无多 DB、无过期）。
- 事务在单进程内通过命令串行执行模拟原子性；没有 WATCH/乐观锁。
- 未实现 inline 命令（裸文本 `PING\r\n`）——只接受数组 of bulk 的标准
  RESP 请求帧，这是有意的严格选择。
- 纯内存存储，进程退出数据即丢失，无持久化/复制。
