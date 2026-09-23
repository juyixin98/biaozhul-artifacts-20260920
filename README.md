# DNS 压缩报文解析代理（Go, net/http, 纯标准库）

一个只做后端的本地 DNS 查询代理：HTTP 接口接收 A/AAAA 查询 → 通过 **UDP** 向
**本地可控的假 DNS 服务器**发出单问题查询 → 安全解析（含名称压缩指针）响应 →
按最小 TTL 缓存成功结果。全程 loopback，**不访问公网**。

实现语言：Go 1.23，仅依赖标准库（`net/http`、`net`、`encoding/binary`、`crypto/rand` 等），
无任何第三方依赖，因此没有 `go.sum`（不存在需要校验和锁定的模块；`go.mod` 即完整的
依赖锁定，见文末「依赖锁定」）。

---

## 1. 目录结构

```
.
├── go.mod                         # 模块声明；无 require、无第三方依赖
├── README.md
├── cmd/
│   ├── dnsproxy/main.go           # HTTP 前端服务（代理本体）
│   └── fakedns/main.go            # 本地可控假上游 UDP DNS 服务器
├── internal/
│   ├── dnsmsg/dnsmsg.go           # DNS 报文构造 + 安全解析（压缩指针）
│   ├── dnsmsg/dnsmsg_test.go      # 解析器安全单测（20 个）
│   ├── cache/cache.go             # TTL 缓存（懒过期 + 后台清扫）
│   ├── cache/cache_test.go        # 缓存/TTL 边界单测（7 个）
│   ├── proxy/proxy.go             # UDP 收发 + 响应校验 + 缓存策略
│   ├── proxy/proxy_test.go        # 代理校验矩阵单测（15 个）
│   ├── httpapi/server.go          # net/http 路由与 JSON
│   ├── httpapi/server_test.go     # HTTP 层单测
│   ├── fakeserver/fakeserver.go   # 假上游：内置区 + 12 种畸形/边界构造
│   └── fakeserver/fakeserver_test.go
└── integration/
    └── integration_test.go        # 真实 UDP loopback + HTTP 端到端测试（23 个）
```

共 **74 个自动化测试**：单元测试（手工构造畸形报文，不依赖网络）+
端到端集成测试（真实 UDP socket、真实 HTTP server）。

---

## 2. 依赖与环境

- Go 1.23+（开发机实测 `go1.23.1 linux/amd64`）。
- 无第三方依赖；构建/测试不需要联网下载模块。
- 仅使用 loopback UDP/TCP，端口可用参数指定。

```bash
go version
# go version go1.23.1 linux/amd64
```

---

## 3. 启动命令

开两个终端（或后台启动两个进程）。

### 终端 1：本地假上游

```bash
go run ./cmd/fakedns -addr 127.0.0.1:1053
# fake upstream listening on udp 127.0.0.1:1053 (zone: 6 records)
```

### 终端 2：HTTP 代理

```bash
go run ./cmd/dnsproxy -listen 127.0.0.1:8080 -upstream 127.0.0.1:1053 -timeout 2s
# HTTP listening on 127.0.0.1:8080, upstream udp 127.0.0.1:1053, timeout 2s
```

也可以先编译再运行：

```bash
go build -o fakedns  ./cmd/fakedns
go build -o dnsproxy ./cmd/dnsproxy
./fakedns  -addr 127.0.0.1:1053
./dnsproxy -listen 127.0.0.1:8080 -upstream 127.0.0.1:1053
```

命令行参数：

| 程序 | 参数 | 默认值 | 说明 |
|---|---|---|---|
| `fakedns` | `-addr` | `127.0.0.1:1053` | UDP 监听地址 |
| `dnsproxy` | `-listen` | `127.0.0.1:8080` | HTTP 监听地址 |
| `dnsproxy` | `-upstream` | `127.0.0.1:1053` | 上游 UDP DNS 地址 |
| `dnsproxy` | `-timeout` | `2s` | 单次上游查询超时 |
| `dnsproxy` | `-sweep` | `10s` | 缓存后台清扫间隔 |

---

## 4. HTTP 接口与请求样例

所有响应均为 JSON。错误响应：`400` 表示请求非法（不产生上游查询），
`502` 表示上游失败或响应未通过校验。

### 4.1 `GET /resolve?name=<域名>&type=<A|AAAA>`

成功（200）：

```jsonc
{
  "name": "example.com",
  "type": "A",
  "source": "upstream",                 // 第二次查询同一名字时为 "cache"
  "answers": [
    {"type": "A", "ip": "192.0.2.10", "ttl": 30}
  ]
}
```

请求样例：

```bash
# A 记录
curl -s 'http://127.0.0.1:8080/resolve?name=example.com&type=A'
# 域名大小写/结尾点不影响命中（代理会规范化为小写、去尾点）
curl -s 'http://127.0.0.1:8080/resolve?name=EXAMPLE.com.&type=A'
# AAAA
curl -s 'http://127.0.0.1:8080/resolve?name=v6.example&type=AAAA'
# type 缺省为 A
curl -s 'http://127.0.0.1:8080/resolve?name=www.example.com'
```

AAAA 响应：

```json
{"name":"v6.example","type":"AAAA","source":"upstream",
 "answers":[{"type":"AAAA","ip":"2001:db8::2","ttl":45}]}
```

客户端错误（400，**不会**发起上游查询）：

```bash
curl -s -w ' [%{http_code}]\n' 'http://127.0.0.1:8080/resolve?name=example.com&type=MX'
# {"error":"type must be A or AAAA","status":400} [400]
curl -s -w ' [%{http_code}]\n' 'http://127.0.0.1:8080/resolve?name=bad..name&type=A'
# {"error":"invalid request: dnsmsg: invalid domain name: empty label in \"bad..name\"","status":400} [400]
```

上游/响应错误（502，**不写缓存**），形如：

```json
{"error":"malformed upstream response: transaction id mismatch: got 8714 want 56821","status":502}
```

### 4.2 `GET /cache`

查看当前存活缓存项与剩余 TTL（秒，向下取整）：

```bash
curl -s http://127.0.0.1:8080/cache
```

```json
{"entries":[{"name":"example.com","qtype":1,"type":"A",
 "answers":[{"type":"A","ip":"192.0.2.10","ttl":30}],
 "ttl_remaining_seconds":27,
 "cached_at":"2026-09-23T15:24:31Z"}]}
```

### 4.3 `DELETE /cache`

清空缓存：

```bash
curl -s -X DELETE http://127.0.0.1:8080/cache
# {"flushed":1}
```

### 4.4 `GET /healthz`

```bash
curl -s http://127.0.0.1:8080/healthz
# {"status":"ok","time":"2026-09-23T15:24:23Z"}
```

---

## 5. 假上游内置数据

正常区（命中即按真实 DNS 线格式应答，回答 owner 用压缩指针 `0xC00C`）：

| 名称 | 类型 | 值 | TTL |
|---|---|---|---|
| `example.com` | A | 192.0.2.10 | 30 |
| `www.example.com` | A | 192.0.2.20 | 15 |
| `longttl.example` | A | 192.0.2.21 | 600 |
| `v6.example` | AAAA | 2001:db8::2 | 45 |
| `multi.example` | A ×2 | 192.0.2.30 / .31 | 20 / 10 |

畸形/边界专用名字（用于验收，见第 7 节）：

`loop.example`、`fwd.example`、`chain.example`、`trunc.example`、
`short.example`、`badid.example`、`nxdomain.example`、`noans.example`、
`tc.example`、`tql0.example`、`ttlmax.example`、`multiq.example`。

---

## 6. 压缩指针安全解析说明

解析核心在 `internal/dnsmsg` 的 `DecodeName`，每次读取都做边界检查，并实施：

1. **只许向后跳**：压缩指针目标必须严格小于当前指针位置且位于报文内。
   指针环（自指、两个指针互指）必然包含一条前向/自指边，直接被拒
   （`ErrForwardPointer` / `ErrBadPointer`）。
2. **跳转上限 + 已访问集合**：接受最多 **127 跳**（第 128 跳拒绝），
   并用 visited 集合做纵深防御，防止任何残留环路造成无限循环
   （`ErrCompressionLoop`）。
3. **全部边界检查**：标签越界、名称 >253 字符（编码 >255 字节）、
   RR 固定字段截断、RDLENGTH 超出报文尾部等全部拒绝
   （`ErrTruncatedLabel` / `ErrTruncatedRR` / `ErrBadRDLength`）。
4. **保留编码拒绝**：长度前缀的 `01`/`10` 保留组合拒绝
   （`ErrReservedLabel`）。
5. **计数上限**：各 section 数量声明超过 4096 直接拒绝，避免恶意计数触发大分配。
6. **查询端**：事务 ID 用 `crypto/rand` 生成；只发 UDP、单问题、IN、A/AAAA。

### 响应入库前的校验链（任一不过即 502 且不入缓存）

- UDP 超时内收到数据报，且 ≥ 12 字节；
- 事务 ID 必须与本代理发出的查询一致；
- `QR=1`、`TC=0`、`RCODE=0`；
- 恰好 1 个问题，且回显的名字/类型/类与请求一致；
- 回答区非空，每条回答类型=请求类型（不支持 CNAME 跟随）、类=IN、
  owner 与问题名一致、rdata 长度正确（A=4 / AAAA=16）。

缓存寿命取所有回答 TTL 的**最小值**；**TTL=0 的应答会正常返回给调用方，但绝不写入缓存**。

---

## 7. 验收矩阵（需求点 → 实测结果）

下列结果来自集成测试 `TestAcceptanceMatrixSummary` 的真实输出（随机查询 ID）：

| 验收输入 | 构造方式 | HTTP | 解析/校验结果 | 是否入缓存 |
|---|---|---|---|---|
| 指针环 `loop.example` | 应答名两个互指指针 | 502 | `compression pointer does not point backwards: target 32 >= position 30` | 否 |
| 越界偏移 `fwd.example` | 指针指向偏移 64（报文仅 31 字节） | 502 | `compression pointer out of bounds: offset 64 in 31-byte message` | 否 |
| 超长后向链 `chain.example` | 127 个后向指针藏于前一个附加 RR 的 rdata，第 128 跳为入口 | 502 | `compression pointer chain too long (loop or >127 jumps)` | 否 |
| 截断响应 `trunc.example` | RDLENGTH=255，实际仅剩 4 字节 | 502 | `rdata length exceeds message: rdlength=255 remaining=4` | 否 |
| 超短数据报 `short.example` | 仅 6 字节 | 502 | `message shorter than 12-byte header: got 6 bytes` | 否 |
| ID 不匹配 `badid.example` | 合法应答但 ID 取反 | 502 | `transaction id mismatch: got 8714 want 56821` | 否 |
| NXDOMAIN | RCODE=3 | 502 | `upstream rcode=3 (NXDOMAIN)` | 否 |
| 空回答 `noans.example` | RCODE=0 但 ANCOUNT=0 | 502 | `rcode=0 but answer section is empty` | 否 |
| 截断位 `tc.example` | TC=1 | 502 | `response has TC (truncation) set` | 否 |
| 多问题 `multiq.example` | QDCOUNT=2 | 502 | `expected 1 question, got 2` | 否 |
| **TTL=0** `tql0.example` | 合法 A，TTL=0 | **200** | 正常返回；`source` 标注 `upstream (ttl=0, not cached)` | **否**（每次都走上游） |
| **TTL 最大值** `ttlmax.example` | 两条 A，TTL 4294967295 与 5 | 200 | 正常返回；缓存寿命取最小值 5s，uint32 秒数无溢出 | 是，5s |
| 正常 A/AAAA | 内置区 | 200 | 首次 `upstream`，TTL 内第二次 `cache`（上游命中计数不再增加） | 是 |
| 缓存到期 | TTL=2s 的记录 | 200 | 约 2.3s 后再次查询变为 `upstream` 并重新计数 | 重新入库 |

补充错误路径（均在自动化测试中验证）：上游不可达 → 502 不入缓存；
非法 type/域名 → 400 不发查询；错误之后查询其他正常名字不受污染。

---

## 8. 自动化测试

```bash
# 全部测试
go test ./...

# 带竞态检测
go test -race ./...

# 详细查看验收矩阵日志
go test ./integration/ -run TestAcceptanceMatrixSummary -v
```

测试分层：

- `internal/dnsmsg`：直接对线格式字节做断言（自指环、互指环、越界指针、
  127/128 跳边界链、截断、RDLENGTH 越界、保留前缀、超长名、构造器往返等）。
- `internal/cache`：注入假时钟验证 TTL 到期、TTL=0/负值不入缓存、
  uint32 最大 TTL（约 136 年）不溢出、剩余 TTL 计算、清空。
- `internal/proxy`：脚本化 UDP 连接，验证 ID 不匹配、截断、TC、NXDOMAIN、
  空回答、CNAME、问题回显不符、上游超时等全部不缓存，TTL=0 返回但不缓存，
  多回答取最小 TTL。
- `integration`：真实启动假 UDP 服务器 + httptest，端到端跑完整验收矩阵，
  并通过假服务器的命中计数证明「缓存命中不再走上游」与「错误响应零缓存」。

---

## 9. 实际运行记录（本仓库交付时）

环境：`go1.23.1 linux/amd64`，构建与测试全程未联网拉取依赖。

```text
$ go vet ./...        # 无输出（通过）
$ gofmt -l .          # 无输出（格式干净）
$ go test -race -count=1 ./...
?   dnscomp-proxy/cmd/dnsproxy    [no test files]
?   dnscomp-proxy/cmd/fakedns     [no test files]
?   dnscomp-proxy/internal/fakeserver [no test files]
?   dnscomp-proxy/internal/httpapi [no test files]
ok  dnscomp-proxy/integration        3.375s
ok  dnscomp-proxy/internal/cache     1.016s
ok  dnscomp-proxy/internal/dnsmsg    1.018s
ok  dnscomp-proxy/internal/proxy     1.016s
```

真实进程手工联调（`fakedns` 于 `127.0.0.1:12053`，`dnsproxy` 于
`127.0.0.1:28080`；选用高位端口是因为开发机上 8080/18080 已被无关进程
mqttserver 等占用）：

```text
GET example.com A        -> 200 source=upstream 192.0.2.10 ttl=30
GET EXAMPLE.com. A       -> 200 source=cache     （上游命中计数保持 1）
GET v6.example AAAA      -> 200 2001:db8::2 ttl=45
GET multi.example A      -> 200 两条记录，TTL 20/10
GET tql0.example A  (×2) -> 两次 200 均 source=upstream (ttl=0, not cached)，缓存始终为空
GET ttlmax.example A     -> 200 两条（4294967295 / 5），/cache 显示剩余 ~4s
GET type=MX              -> 400 type must be A or AAAA
GET name=bad..name       -> 400 invalid domain name
DELETE /cache            -> {"flushed":n}；随后 GET /cache 为空
loop/fwd/chain/trunc/short/badid/nxdomain/noans/tc/multiq
                         -> 全部 502，错误信息见第 7 节，GET /cache 始终为空
```

TTL 到期的端到端证据由集成测试 `TestCacheTTLExpiry` 自动给出（记录 TTL=2s，
sleep 2.3s 后确认来源由 `cache` 变回 `upstream`、上游命中计数由 1 变 2）。

---

## 10. 依赖锁定

- `go.mod` 声明模块与 `go 1.23` 指令；**没有任何 `require`**，即零第三方依赖。
- 因此不存在 `go.sum`（`go mod tidy` 不会生成它；没有需要锁定校验和的模块）。
- 可重复构建由 Go 工具链与标准库版本保证；如需严格复现，固定使用 Go 1.23.x。

---

## 11. 明确的范围限制 / 未完成项

为贴合题目范围（UDP、A/AAAA、单问题），以下能力**有意不做**：

1. **不支持 TCP / TLS / DoH / DoT**，也不做 UDP 截断后的 TCP 回退；
   收到 `TC=1` 直接报错。
2. **不跟随 CNAME 链**：回答里出现非请求类型（如 CNAME）即判为畸形。
3. **不支持 EDNS(0)**：按经典 512 字节 UDP 限制接收（`MaxUDPSize` 默认 512）。
4. **仅单问题**：请求只构造 1 个 question；响应问题数 ≠ 1 直接拒绝。
5. **无负缓存**：NXDOMAIN/SERVFAIL 等不缓存（题目只要求成功结果按 TTL 缓存，
   且要求错误响应不得入缓存）。
6. **缓存为进程内单实例**：不持久化、不做多副本共享；重启后清空。
7. 权威区/假服务器仅用于本地验收，不具备真正权威服务器的完整语义。
