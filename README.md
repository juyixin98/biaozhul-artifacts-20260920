# socks5loop — 回环专用 SOCKS5 CONNECT 代理（Go 标准库）

一个只监听 **回环地址** 的 SOCKS5 代理，仅支持 `CONNECT` 命令，仅接受
**预置用户名/密码** 认证（RFC 1928 / RFC 1929），目标地址限定在**本地测试白名单**内。
附带一个基于 `net/http` 的只读控制面（健康检查、流量统计、白名单查看）。

纯 Go 标准库实现，**无任何第三方依赖**（无 `go.sum`；Go 工具链版本由 `go.mod` 锁定）。

## 功能与协议范围

| 项目 | 支持情况 |
| --- | --- |
| 监听地址 | 仅回环：`127.0.0.0/8` 或 `::1`；绑定非回环地址会直接报错退出 |
| SOCKS 版本 | SOCKS5（`VER=0x05`），拒绝其它版本 |
| 命令 | 仅 `CONNECT (0x01)`；`BIND`/`UDP ASSOCIATE` 返回 `0x07 Command not supported` |
| 地址类型 | IPv4 (`0x01`)、域名 (`0x03`)、IPv6 (`0x04`) |
| 认证 | 仅用户名/密码 (`0x02`)；未提供该方法时回 `0xFF`，凭据错误回 `0x01` 并断开 |
| 凭据校验 | `crypto/subtle.ConstantTimeCompare` 常量时间比较 |
| 目标白名单 | CIDR + 主机名后缀；域名先校验，未显式放行的域名解析后**所有** IP 都须在白名单（防 DNS 重绑定），随后用校验过的 IP 字面量拨号 |
| 默认放行 | `127.0.0.0/8`、`::1/128`、`169.254.0.0/16`；主机名 `localhost`、`*.localhost`、`*.example.com` |
| 双向半关闭 | 每个方向独立 pump；一侧收到 EOF 后对目标 `CloseWrite()`，反向数据继续透传 |
| 背压 | 直接 `io.Copy` 到 TCP 连接，不做应用层缓冲；客户端不读时写阻塞，内核反压沿链路传导，字节计数随成功的 `Write` 实时增长 |
| 资源释放 | 握手有总超时；隧道结束（双向 EOF）后 `WaitGroup` 汇合，两端连接 `defer Close()`；`active_sessions` 归零 |
| 控制面 | `GET /healthz`、`GET /stats`、`GET /allowlist`（同样只绑定回环） |

## 目录结构

```
cmd/socks5proxy/main.go     程序入口、flag、信号优雅退出、回环绑定校验
internal/socks5/protocol.go RFC 报文解析（方法/认证/请求），逐字节读取
internal/socks5/server.go   握手状态机、CONNECT、拨号、双向桥接、半关闭、统计
internal/socks5/allowlist.go 白名单（CIDR/后缀/解析校验、4in6 解映射）
internal/httpapi/httpapi.go net/http 控制面
scripts/smoke.sh            端到端冒烟脚本（curl + 本地源站）
```

## 环境与依赖

- Go（在 **go1.23.4 linux/amd64** 上开发与测试；`go.mod` 声明 `go 1.23`）
- 运行时仅需 Go 标准库；冒烟脚本另外用到 `curl`（建议 7.x+，支持 SOCKS5）与 `python3`（仅起本地测试源站）
- Linux/macOS。IPv6 用例需要本机 `::1` 可用（不可用时测试自动 skip）

## 启动命令

```bash
# 编译
go build -o bin/socks5proxy ./cmd/socks5proxy

# 启动（凭据用 flag 或环境变量，二者必给，匿名访问一律拒绝）
./bin/socks5proxy \
  --username alice --password 's3cret!' \
  --listen 127.0.0.1:1080 \
  --http   127.0.0.1:8080

# 也可用环境变量传凭据
SOCKS5_USER=alice SOCKS5_PASS='s3cret!' ./bin/socks5proxy
```

全部参数：

| flag | 默认值 | 说明 |
| --- | --- | --- |
| `--listen` | `127.0.0.1:1080` | SOCKS5 监听地址（强制回环） |
| `--http` | `127.0.0.1:8080` | HTTP 控制面地址（强制回环） |
| `--username` / `--password` | 空（必填） | SOCKS5 凭据，也读 `SOCKS5_USER`/`SOCKS5_PASS` |
| `--allow-cidrs` | 空 | 逗号分隔，**追加**到默认 CIDR 白名单 |
| `--allow-hosts` | 空 | 逗号分隔，**追加**到默认主机名后缀白名单 |
| `--handshake-timeout` | `10s` | 方法+认证+请求整段握手的截止时间 |
| `--dial-timeout` | `10s` | 连接目标的拨号超时 |

追加白名单示例：`--allow-cidrs 10.0.0.0/24 --allow-hosts .svc.test,build.local`。

> 安全说明：命令行参数在本机可能通过进程列表可见；生产用法建议走环境变量。

## HTTP 接口与请求样例

控制面是普通 HTTP/JSON（不是代理流量端口）：

```bash
curl -s http://127.0.0.1:8080/healthz
# {"status":"ok"}

curl -s http://127.0.0.1:8080/stats
# {"accepts":6,"active_sessions":0,"auth_failures":1,"rejected_by_allowlist":1,
#  "connect_ok":2,"dial_failures":2,"bytes_client_to_target":156,"bytes_target_to_client":436}

curl -s http://127.0.0.1:8080/allowlist
# {"cidrs":["127.0.0.0/8","::1/128","169.254.0.0/16"],
#  "hosts":["localhost",".localhost",".example.com"]}
```

## SOCKS5 请求样例

### curl（最常用）

```bash
# 经代理访问本地源站（IPv4）
curl -v --socks5-hostname "alice:s3cret!@127.0.0.1:1080" http://127.0.0.1:8000/

# IPv6 回环目标
curl -g --socks5-hostname "alice:s3cret!@127.0.0.1:1080" "http://[::1]:8000/"

# 域名目标（ATYP=DOMAIN，由代理解析）
curl --socks5-hostname "alice:s3cret!@127.0.0.1:1080" http://localhost:8000/

# 错误凭据 / 白名单外目标 —— 都应失败（curl 退出码 97）
curl --socks5-hostname "nope:wrong@127.0.0.1:1080"        http://127.0.0.1:8000/
curl --socks5-hostname "alice:s3cret!@127.0.0.1:1080"     http://example.com/
```

`--socks5-hostname` 让 curl 发送域名 ATYP；`--socks5` 则本地解析后发 IP 字面量。

### 裸字节握手（Python，便于对照协议）

```python
import socket, struct
c = socket.create_connection(("127.0.0.1", 1080))
c.sendall(bytes([5, 2, 0, 2]))                       # VER NMETHODS METHODS[NOAUTH, USERPASS]
c.recv(2)                                            # -> 05 02（选择用户名/密码）
u, p = b"alice", b"s3cret!"
c.sendall(bytes([1, len(u)]) + u + bytes([len(p)]) + p)
c.recv(2)                                            # -> 01 00（认证成功；失败为 01 01）
c.sendall(bytes([5, 1, 0, 1]) +
          socket.inet_aton("127.0.0.1") +
          struct.pack("!H", 8000))                   # CONNECT 127.0.0.1:8000
c.recv(10)                                           # -> 05 00 00 01 <BND.ADDR> <BND.PORT>
c.sendall(b"GET / HTTP/1.0\r\n\r\n")
```

### 一键冒烟

```bash
go build -o bin/socks5proxy ./cmd/socks5proxy
./bin/socks5proxy --username alice --password 's3cret!' \
  --listen 127.0.0.1:11080 --http 127.0.0.1:18080 &
SOCKS5_ADDR=127.0.0.1:11080 HTTP_ADDR=127.0.0.1:18080 ./scripts/smoke.sh
```

## 自动化测试

```bash
go test ./...                # 全部用例
go test -race ./...          # 竞态检测（验收时使用）
go test -v -run TestHalfPacketHandshake ./internal/socks5/
```

验收点到测试的对应关系：

| 验收要求 | 测试 |
| --- | --- |
| 半包握手 | `TestHalfPacketHandshake`（握手每个字节间隔 2ms 发送）；解析层 `TestReadMethodsOneByteAtATime`、`TestReadAuthOneByteAtATime`、`TestReadRequestIPv6Fragmented`（自研一次一字节 reader）；截断报文 `TestReadMethodsTruncated` |
| 未支持命令 | `TestUnsupportedCommand`（BIND 收到 REP `0x07`）；另含错误 ATYP、错误版本/保留位等协议负例 |
| 认证失败 | `TestAuthFailure`（REP `0x01` 后连接被关闭）、`TestNoAcceptableMethod`（回 `05 FF`） |
| 双向半关闭 | `TestBidirectionalHalfClose`：客户端只 `CloseWrite`，目标在读到 EOF 后才回写全部 11KB，客户端仍能完整收完 |
| 慢读背压 + 资源释放 | `TestSlowReaderBackpressureAndRelease`：目标灌 64MiB，客户端先完全不读，断言 `/stats` 字节数在充满管道后**平台化**（低于 12MiB 且连续零增长），随后全速排空并校验逐字节正确，最后 `active_sessions` 归零；`TestTruncatedHandshakeTimesOut` 验证握手卡死会被超时关闭且会话计数归零 |
| IPv4 / IPv6 / 域名 | `TestEndToEndIPv4`、`TestEndToEndIPv6`（`::1`，无 IPv6 环境自动 skip）、`TestEndToEndDomainLocalhost` |
| 白名单 | `TestDestinationNotAllowed`（8.8.8.8 REP `0x02`）、`TestConnectionRefusedReply`（REP `0x05`）、`allowlist_test.go`（后缀规则、4in6、DNS 重绑定混合应答拒绝、坏配置报错） |
| 回环限定 | `TestRefusesNonLoopbackBind`（绑定 `0.0.0.1` 必须失败） |
| 并发与接口 | `TestConcurrentSessions`（32 路并发 echo）、`TestHTTPControlAPI`、`TestHTTPEndToEndThroughProxy` |

## 实测结果（本仓库开发环境）

环境：Ubuntu 24.04.4，Linux 6.8，go1.23.4 linux/amd64，curl 8.5.0。

- `go vet ./...`：无输出（通过）；`gofmt -l .`：无输出。
- `go test -count=1 -race ./...`：**PASS**（`ok socks5loop/internal/socks5`）。
- 背压用例连续 `-count=10 -race` 运行：**10/10 通过**；该用例约 2s（64MiB 负载）。
- `scripts/smoke.sh` 实跑：IPv4 与域名 `localhost` 取回 200；白名单外 `example.com` 与错误凭据均被拒（curl 97）；`/healthz`、`/stats`、`/allowlist` 返回正常 JSON。
- 裸字节握手实测：`0502` → `0100` → CONNECT REP `00`，随后 `HTTP/1.0 200 OK`。
- IPv6 实测：经代理访问 `http://[::1]:.../index.html` 返回 `hello via ipv6 loopback`。

关键设计取舍：

- 背压用例**不缩小 socket 缓冲区**（早期版本把两端缓冲设为 4KiB，在 Linux 延迟 ACK 下排空 1MiB 要 ~26s）。
  改为 64MiB 负载 + 统计平台化判定，在默认 `tcp_rmem`/`tcp_wmem` 自动调优上限（本机 6MiB/4MiB）下既稳定又快。
  该阈值假设系统未调高这些 sysctl；若机器把缓冲调到 12MiB 以上需相应调大断言。
- 字节计数放在**成功的 `Write`** 上（而非 `io.Copy` 返回时一次性计数），否则背压期间统计恒为 0，无法观测平台期——这是开发中实测发现并修正的一个真实缺陷。
- 白名单对域名采取“解析后**所有**地址都须合法 + 用校验后的 IP 拨号”，避免 DNS 重绑定到内网地址。

## 未完成 / 已知限制（如实记录）

1. 仅实现 CONNECT；**BIND 与 UDP ASSOCIATE 明确不支持**（返回 `0x07`/不处理），这是范围裁剪而非缺陷。
2. 用户名/密码报文按 RFC 1929 长度字段上限为 255 字节；不支持链式 SOCKS、GSSAPI、SOCKS4。
3. 域名后缀白名单命中后直接放行域名（依赖本机解析器）；仅对“未显式放行”的域名做解析后全 IP 校验。默认后缀含 `localhost`/`*.localhost`。
4. 控制面没有鉴权——因为它与代理一样**只绑定回环**，信任边界为本机用户；不要把 `--http` 指向非回环地址（程序也会拒绝）。
5. 背压测试的字节阈值（12MiB）依赖默认 TCP 缓冲上限；在调高过 `net.ipv4.tcp_{r,w}mem` 的机器上可能需要调大。
6. 未提供 systemd 单元/打包文件与 Dockerfile（题目要求纯后端源码，运行用脚本即可复现）。
7. 日志使用标准库 `slog` 文本输出到 stderr，未做结构化日志落盘或指标导出（`/stats` 已可供抓取）。
