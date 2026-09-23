# DNS 压缩报文解析代理（Go / net/http）

本地 DNS 查询代理，纯后端、无界面。对外提供 HTTP 接口，对内仅通过 **UDP** 向上游
发起 **单问题 A/AAAA** 查询；安全解析名称压缩指针，维护带 TTL 的内存缓存。
上游使用**本地可控的假 DNS 服务器**，整个系统不访问公网。

## 功能与约束

- 仅 UDP 查询上游；不实现 TCP 回退。
- 仅支持单问题、IN 类、A / AAAA；其它类型在入口即被拒绝。
- 安全的域名解析（RFC 1035 4.1.4 压缩指针）：
  - 指针只能指向**报文内、头部之后、先前出现**的位置；
  - 自指针（单节点环）→ `ErrPointerLoop`；前向指针（构造互指环的必要环节）→ `ErrForwardPointer`；
  - 指向报文之外 → `ErrPointerOutOfBounds`；`10/01` 保留标签类型 → `ErrReservedLabelType`；
  - 跳转次数上限（64）作为纵深防御；名称 ≤ 253 字符（线格式 255 字节）、标签 ≤ 63；
  - 各类物理截断（不足头部、固定字段不完整、RDLENGTH 越界）→ `ErrShortMessage` / `ErrBadRDLength`。
- 响应严格校验后才采信：QR、OPCODE、TC、事务 ID、问题名/类型/类、应答 TTL（最高位不得为 1）。
  TC=1（截断）在 UDP-only 模式下直接拒绝。
- 每次查询使用独立本地 socket，过期/无关数据报天然无法串入。
- 缓存：
  - 仅缓存 `RCODE=0` 且含匹配 A/AAAA 地址、最小 TTL > 0 的响应；
  - **错误响应不入缓存**：畸形报文、ID 不匹配、TC、NXDOMAIN/SERVFAIL、空答案、TTL=0 均不缓存；
  - 命中时按已过时间重算剩余 TTL；到点惰性失效。

## 目录结构

```
cmd/dnsproxy/          HTTP 服务入口
cmd/fakedns/           本地静态分区假 DNS 上游（UDP，手动联调用）
internal/dnsmsg/       DNS 报文构建/安全解析（压缩指针、截断处理）
internal/dnscache/     带 TTL 的并发安全内存缓存
internal/fakedns/      可编程假上游（测试用，可构造任意应答）
internal/proxy/        查询编排：缓存→UDP 上游→响应校验→入缓存
internal/httpapi/      net/http 路由与 JSON
```

## 依赖

- Go **1.23.1**（go.mod 声明 `go 1.23.1`）。
- **零第三方依赖**，仅使用 Go 标准库（`net`、`net/http`、`encoding/binary` 等）。
  因此没有 `go.sum`——不存在可漂移的外部版本，这本身即依赖锁定。
- Linux/macOS，回环 UDP。

## 构建

```bash
go build -o bin/dnsproxy ./cmd/dnsproxy
go build -o bin/fakedns  ./cmd/fakedns
# 或
make build
```

## 启动

需要两个进程（两个终端）：

```bash
# 终端 1：本地假 DNS 上游（默认 udp 127.0.0.1:5354）
./bin/fakedns -listen 127.0.0.1:5354

# 终端 2：HTTP 代理（默认 127.0.0.1:8080，上游指向假服务器）
./bin/dnsproxy -listen 127.0.0.1:8080 -upstream 127.0.0.1:5354 -timeout 2s
```

参数：

| 程序 | 参数 | 默认值 | 说明 |
|---|---|---|---|
| `dnsproxy` | `-listen` | `127.0.0.1:8080` | HTTP 监听地址 |
| `dnsproxy` | `-upstream` | `127.0.0.1:5354` | UDP DNS 上游 |
| `dnsproxy` | `-timeout` | `2s` | 等待上游响应超时 |
| `fakedns` | `-listen` | `127.0.0.1:5354` | UDP 监听地址 |

## HTTP 接口

### `GET /resolve?name=<域名>&type=A|AAAA`

`type` 可省略（默认 A）。成功返回 200：

```json
{
  "name": "example.com.",
  "type": "A",
  "rcode": 0,
  "answers": [
    {"name": "example.com.", "type": "A", "ttl": 60, "value": "192.0.2.10"},
    {"name": "example.com.", "type": "A", "ttl": 60, "value": "192.0.2.11"}
  ],
  "ttl": 60,
  "cached": false
}
```

- `ttl`：上游应答中匹配记录的**最小** TTL；缓存命中时为剩余 TTL。
- `cached`：本次结果是否来自缓存。
- NXDOMAIN 等合法 DNS 错误仍是 HTTP 200，靠 `rcode` 区分（`answers` 为空）。
- HTTP 状态：`400` 入参非法（缺 name、类型非 A/AAAA、域名非法）；
  `502` 上游连接/解析/校验失败（畸形、ID 不匹配、TC、名称不一致等）；
  `504` 等待上游超时；`405` 方法不允许。

### `GET /cache`

```json
{"entries": 4}
```

### `POST /cache/flush`

清空缓存，返回 `{"status":"flushed"}`。

### `GET /healthz`

存活探针，返回 `{"status":"ok"}`。

## 请求样例（curl）

```bash
# A 记录（查两次观察 cached）
curl -s 'http://127.0.0.1:8080/resolve?name=example.com&type=A'
curl -s 'http://127.0.0.1:8080/resolve?name=EXAMPLE.COM.&type=A'

# AAAA
curl -s 'http://127.0.0.1:8080/resolve?name=example.com&type=AAAA'

# TTL=0：cached 恒为 false，每次都访问上游
curl -s 'http://127.0.0.1:8080/resolve?name=zero.example.com&type=A'

# NXDOMAIN（rcode=3，不入缓存）
curl -s 'http://127.0.0.1:8080/resolve?name=nx.example.com&type=A'

# 缓存统计 / 清空 / 健康
curl -s 'http://127.0.0.1:8080/cache'
curl -s -X POST 'http://127.0.0.1:8080/cache/flush'
curl -s 'http://127.0.0.1:8080/healthz'

# 错误样例
curl -i 'http://127.0.0.1:8080/resolve?name=example.com&type=MX'   # 400
curl -i 'http://127.0.0.1:8080/resolve'                            # 400
```

内置假服务器分区（见 `cmd/fakedns/main.go`）：

| 名称 | 类型 | 值 | TTL |
|---|---|---|---|
| `example.com` | A | 192.0.2.10, 192.0.2.11 | 60 |
| `example.com` | AAAA | 2001:db8::10 | 30 |
| `www.example.com` | A | 192.0.2.20 | 5 |
| `zero.example.com` | A | 192.0.2.30 | 0 |
| `long.example.com` | A | 192.0.2.40 | 2147483647（最大合法值） |
| `nx.example.com` | — | NXDOMAIN | — |

（地址使用 RFC 5737/3849 文档保留网段，仅用于本地演示。）

## 测试

```bash
go test -count=1 ./...      # 或 make test
go test -race -count=1 ./... # 竞态检测（或 make test-race）
```

### 验收点 → 测试对照

| 验收要求 | 测试 |
|---|---|
| 指针环 | `dnsmsg: TestPointerLoopSelf` / `TestPointerLoopMutual`；`proxy: TestUpstreamPointerLoop` |
| 越界偏移 | `dnsmsg: TestPointerOutOfBounds`（报文外/头部/0xFFFF）；`proxy: TestUpstreamPointerOutOfBounds` |
| 截断响应 | `dnsmsg: TestTruncatedResponses`；`proxy: TestUpstreamTruncatedRDLength`、`TestUpstreamTCBit` |
| ID 不匹配 | `proxy: TestUpstreamIDMismatch` |
| TTL 边界 | `dnscache: TestSetGetAndExpiry` / `TestTTLZeroNotCached` / `TestTTLMaxBoundary`；`proxy: TestTTLZeroNotCached` / `TestTTLMaxAndMSB` |
| 错误响应不入缓存 | 上述每个 proxy 错误用例后均断言 `Cache().Len()==0`；`TestUpstreamNXDOMAINNotCached` |
| 正常压缩/多跳链 | `dnsmsg: TestParseValidCompressionPointer`、`TestParseMultiHopPointerChain` |
| 缓存命中/过期 | `proxy: TestResolveHappyPathAndCacheHit`、`TestCacheExpiresPerTTL` |

## 设计说明（安全要点）

1. **名称解析**是逐字节的边界检查状态机；所有长度/偏移都与 `len(msg)` 比较后再切片，
   杜绝越界 panic。压缩指针用“只许向后指”这一 RFC 规则根除环路，另设跳转计数兜底。
2. **查询/响应隔离**：每个查询新建 `DialUDP` 连接，只在其上等待一次匹配响应；
   事务 ID 再校验一次，迟到或伪造的数据报不会被接受。
3. **缓存只信成功**：任何传输错误、协议校验失败、非 0 RCODE、空答案、TTL=0 都不写缓存，
   避免负缓存/毒化。TTL 最高位为 1（语义有歧义）按无效应答拒绝。
4. 缓存、假服务器均为并发安全；假服务器每包独立 goroutine 处理。

## 已知限制 / 未完成项

- UDP-only：不跟随 TC 走 TCP，也不做 EDNS（OPT 记录会被当作普通 RR 解析但不使用）。
- 不实现递归/迭代、DNSSEC、DoH/DoT、IPv6 传输链路选择。
- 缓存为进程内内存，无持久化、无容量上限（按名称键惰性过期），重启即清空。
- 未实现并发同键请求合并（singleflight）——高并发下缓存未命中会同时打向上游。
- HTTP 服务仅做请求日志，未加认证/TLS，定位为本地工具，请勿暴露到不可信网络。
- 未提供 DNS-over-UDP 的对外监听端口（对外只有 HTTP）。
