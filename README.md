# socks5proxy — 回环 SOCKS5 CONNECT 代理（握手 + 中继）

纯后端实现：一个仅监听回环地址的 SOCKS5 CONNECT 代理（RFC 1928 + RFC 1929
用户名/密码认证），附带一个 `net/http` 状态查询 HTTP 接口。无任何第三方依赖，
仅使用 Go 标准库。

## 功能

- 仅监听回环地址（`127.0.0.1` / `::1` / `localhost`）；对非回环监听地址直接拒绝启动。
- 认证方法**只接受用户名/密码**（方法 `0x02`），凭据为启动时预置的一组；
  客户端不提供该方法时回 `0xFF`（无可接受方法）并断开。
- 仅支持 `CONNECT` 命令；`BIND`/`UDP ASSOCIATE` 等回 `0x07`（命令不支持）。
- 目标地址支持 IPv4（`0x01`）、域名（`0x03`）、IPv6（`0x04`）；其他地址类型回 `0x08`。
- 目标白名单（本地测试用途）：
  - 主机：仅 `127.0.0.0/8`、`::1`、`localhost`（及 `-allow-hosts` 额外指定的主机名）；
  - 端口：默认不限，可用 `-allow-ports "80,8080,9000-9010"` 限定。
  - 不在白名单回 `0x02`（不允许）并断开。
- 握手阶段整体读超时（默认 10s，`-handshake-timeout`），防止半包连接占用资源。
- 双向中继支持 TCP 半关闭：一端 EOF 后只对对端 `CloseWrite`，反向数据继续传输，
  两个方向都结束后连接才完全释放。
- 背压：中继使用固定 32KB 缓冲的 `io.Copy`，慢读消费方会自然阻塞对端发送，
  代理本身不堆积数据。
- HTTP 状态接口（`net/http`，同样仅回环）：
  - `GET /healthz` → `ok`
  - `GET /stats` → JSON 计数器（活动/累计连接、握手失败、认证失败、拨号失败、白名单拒绝、已中继）
  - `GET /whitelist` → 当前生效的白名单

## 依赖与构建

- Go ≥ 1.23（开发用 go1.23.1）。仅标准库，`go.mod` 无外部依赖，无需额外锁定文件。
- 目录布局：
  - `cmd/socks5d/main.go` — 可执行入口（参数解析、HTTP 接口、信号处理）
  - `internal/socks5/server.go` — 协议实现（协商/认证/请求/中继）
  - `internal/socks5/server_test.go` — 自动化测试
  - `examples/demo.sh` — 端到端演示脚本

```sh
go build ./...          # 编译
go test -race ./...     # 运行全部测试（含竞态检测）
go build -o socks5d ./cmd/socks5d
```

## 启动

```sh
./socks5d \
  -socks-addr 127.0.0.1:1080 \   # SOCKS5 监听地址（回环）
  -http-addr  127.0.0.1:8080 \   # HTTP 状态接口地址（回环）
  -user testuser -pass testpass \# 预置用户名密码（唯一接受的凭据）
  -allow-ports 9000 \            # 可选：目标端口白名单，空=不限
  -handshake-timeout 10s         # 可选：握手阶段整体超时
```

## 请求样例

先起一个本地目标服务：`python3 -m http.server 9000 --bind 127.0.0.1`

```sh
# HTTP 状态接口
curl http://127.0.0.1:8080/healthz
curl http://127.0.0.1:8080/stats
curl http://127.0.0.1:8080/whitelist

# 通过代理访问（IPv4 目标）
curl --socks5 testuser:testpass@127.0.0.1:1080 http://127.0.0.1:9000/

# 通过代理访问（域名目标，代理解析 localhost）
curl --socks5-hostname testuser:testpass@127.0.0.1:1080 http://localhost:9000/

# 认证失败 / 目标不在白名单：curl 退出码 97（代理握手失败）
curl --socks5 testuser:wrong@127.0.0.1:1080 http://127.0.0.1:9000/
curl --socks5 testuser:testpass@127.0.0.1:1080 http://8.8.8.8:9000/
```

也可以直接运行 `bash examples/demo.sh` 一键完成上述演示。

## 测试覆盖（对应验收项）

| 验收项 | 测试 |
|---|---|
| 半包握手（逐字节发送 greeting/auth/request） | `TestHalfPacketHandshake` |
| 半包后停滞被超时清理、连接释放 | `TestHandshakeTimeout` |
| 未支持命令（BIND → 0x07）、未支持地址类型（→ 0x08） | `TestUnsupportedCommand` / `TestUnsupportedAddressType` |
| 认证失败（→ 0x01 并断开连）、无可接受方法（→ 0xFF） | `TestAuthFailure` / `TestNoAcceptableMethods` |
| IPv4 / IPv6 / 域名 CONNECT | `TestConnectIPv4` / `TestConnectIPv6` / `TestConnectDomain` |
| 白名单（非回环主机、域名、端口限定） | `TestWhitelistRejectsNonLoopback` / `TestWhitelistPortRestriction` |
| 目标拒连映射 0x05 | `TestDialRefused` |
| 双向半关闭（客户端半关后仍能收到目标尾声数据；目标半关后客户端读到 EOF） | `TestHalfClose` |
| 慢读背压（8MiB 经代理完整到达慢消费者）+ 连接资源释放 | `TestSlowReaderBackpressure` / `TestConnectionResourceRelease` |
| 拒绝监听非回环地址 | `TestServeRejectsNonLoopback` |

## 实测记录（2026-09-23，go1.23.1 linux/amd64）

- `go vet ./...`、`gofmt`：无告警。
- `go test -race -v -count=1 ./...`：**16/16 全部 PASS**，无 SKIP，无数据竞争。
- 端到端实测（`-user demo -pass secret -allow-ports 9000`，目标为本地
  `python3 -m http.server 9000`）：
  - IPv4 与 `localhost` 域名 CONNECT：均返回 `HTTP 200`；
  - 错误密码、非白名单端口、非白名单主机：curl 退出码均为 97（代理拒绝），
    `/stats` 显示 `auth_failures=1`、`rejected_targets=2`、`relayed_connections=2`；
  - 演示结束后 `active_connections=0`，连接全部释放。

## 限制 / 未完成项

- 仅实现 CONNECT；BIND 与 UDP ASSOCIATE 按协议回 0x07，未实现。
- 凭据为命令行预置单组，未做多用户/外部认证源；明文参数会出现在进程列表中，
  生产使用需改为环境变量或配置文件。
- 域名目标代理由代理解析 DNS；白名单按字面主机名匹配，不解析后校验
  （例如把 `localhost` 之外的域名解析到回环不会自动放行，需在 `-allow-hosts` 中显式列出）。
- 慢读背压通过固定缓冲 + TCP 流控结构性保证，测试验证了数据完整性与连接释放，
  未直接断言代理进程内存上界。
