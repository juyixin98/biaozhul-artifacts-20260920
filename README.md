# Modbus 写入回执 (Modbus Write Receipt)

纯后端实现：一个本地 **Modbus TCP 测试服务器**、一个可编程的**回放客户端**，以及写入操作的 **SQLite 加密回执日志**。Go 标准库 TCP + 纯 Go SQLite 驱动（`modernc.org/sqlite`，不需要 CGO）。

> 关于“密码操作必须真实执行”：Modbus 协议本身**没有任何加密回执或跨连接幂等机制**（见 [协议说明](#协议行为与边界)）。本项目不虚称标准协议具备这些性质，而是在服务端**真实实现**了一层独立的加密审计回执：每次成功的 FC10 写入在 SQLite 中生成一条带 **SHA-256 哈希链 + HMAC-SHA256 认证码**的回执，并提供可复现的篡改检测。HMAC 密钥只保存在服务端，绝不在 Modbus 报文中传输。

## 功能清单

| 要求 | 实现 |
|---|---|
| 明确支持的功能码 | FC `0x03` 读保持寄存器、FC `0x10` 写多个寄存器；其余功能码返回异常 `0x01` |
| MBAP 校验 | Length 字段、Protocol ID（必须为 0）、事务 ID（原样回显）、单元 ID 白名单 |
| 地址范围校验 | 读/写均校验；写批次中**任一寄存器越界则整次写失败**，已在范围内的字也不会被改动 |
| TCP 分片/粘包 | 先读满 7 字节 MBAP 头，再按 Length 读完整 PDU；1 字节/段与多 ADU 合并均正常 |
| 并发一致性 | `sync.RWMutex`：读快照在同一 RLock 内复制，读端永远看不到半批写入；写批次与 SQLite 落盘在同一临界区内，落盘失败回滚寄存器 |
| 断连行为 | 半包后 FIN/RST：丢弃该连接、请求不生效；MBAP 非法（Length/Protocol 错）直接关连接（无可信事务 ID，无法回异常） |
| 异常码 | `0x01/0x02/0x03/0x04/0x0B`，全部写入 SQLite `exceptions` 审计表 |
| 重复事务 ID | **不做去重**：两次同事务 ID 的 FC10 都会执行、都产生回执（遵循标准），示例明确演示 |
| 加密回执 | 每条成功写入：body SHA-256、链式 SHA-256（含创世块）、HMAC-SHA256；`verify` 子命令全链重算，支持篡改/删行/错钥检测 |
| 测试 | Go 单元/并发/竞态测试 + JSON 场景脚本 + 一键验收脚本（含负向用例） |

## 目录结构

```
.
├── cmd/
│   ├── modbus-server/        # TCP 服务器
│   └── modbus-replay/        # 回放/测试客户端（write/read/frag/run/verify/list）
├── internal/
│   ├── protocol/protocol.go  # MBAP 分帧、FC03/FC10 编解码、异常响应
│   ├── register/store.go     # 保持寄存器镜像，整批原子写 + 回滚
│   ├── receipt/store.go      # SQLite、哈希链、HMAC、审计表、全链校验
│   ├── server/server.go      # accept 循环、连接处理、功能码分发
│   └── replay/               # 可控 TCP 客户端 + JSON 脚本执行器
├── examples/*.json           # 验收场景（异常码/粘包/分片/重复事务ID/断连）
├── scripts/acceptance.sh     # 一键端到端验收
└── Makefile
```

## 快速开始

要求 Go 1.22+（无需 C 编译器；构建在有/无 gcc 的环境都可复现）。

```bash
# 1. 构建
make build

# 2. 准备 HMAC 密钥（生产环境请使用随机密钥并妥善保管）
printf 'dev-only-key-change-me' > hmac.key

# 3. 启动服务器（默认 :1502，100 个保持寄存器，单元 ID 白名单=1）
./bin/modbus-server -listen 127.0.0.1:1502 -db receipts.db \
    -registers 100 -units 1 -key-file hmac.key
# 密钥也可以用环境变量：MODBUS_RECEIPT_KEY=... ./bin/modbus-server ...
```

另一个终端：

```bash
# FC10 写多个寄存器（地址40起，三个值，支持 0x 十六进制）
./bin/modbus-replay write -addr 127.0.0.1:1502 -unit 1 -txn 1 \
    -at 40 -values 0x0A0B,0x0C0D,1000

# FC03 读保持寄存器
./bin/modbus-replay read  -addr 127.0.0.1:1502 -unit 1 -txn 2 \
    -at 39 -qty 5

# 分片写（每个 TCP 段 1 字节，段间 2ms）
./bin/modbus-replay frag  -addr 127.0.0.1:1502 -unit 1 -txn 3 \
    -at 50 -values 1,2,3,4 -frag-size 1 -gap 2ms

# 查看回执 / 异常审计
./bin/modbus-replay list -db receipts.db -table receipts -n 10
./bin/modbus-replay list -db receipts.db -table exceptions -n 10

# 校验加密回执链
./bin/modbus-replay verify -db receipts.db -key-file hmac.key
# CHAIN OK: 3 receipt(s), head=ab12...
```

## 自动化验收

```bash
make test          # 全部 Go 测试（含 -count=1 禁用缓存）
make test-race     # 竞态检测
make demo          # 端到端：起真实服务器 → 跑全部 examples → CLI → 链校验 → 篡改负例
```

`make demo`（即 `scripts/acceptance.sh`）会：

1. 构建两个二进制；
2. 在临时目录用全新 SQLite DB 启动服务器；
3. 逐个执行 `examples/` 场景：
   - `00-read-write.json` — 正常读写、地址边界；
   - `01-exceptions.json` — 异常码 `0x01/0x02/0x03/0x0B` 及“越界整批失败后寄存器未变”；
   - `02-coalesce.json` — 三个 ADU 一次 `write()` 的粘包；
   - `03-fragment.json` — FC10 按 1 字节/段分片；
   - `04-duplicate-txn.json` — 同事务 ID 7 写两次，两次都生效、两条回执；
   - `05-disconnect.json` — 半包 FIN/RST、非法 MBAP Length/Protocol 后连接被关闭、服务器存活；
4. 直接跑 CLI 读写与分片；
5. 用正确密钥校验链通过、错误密钥退出码 4；
6. 直接篡改 SQLite 一行后校验必须失败（退出码 4）。

场景文件 JSON 字段见 `internal/replay/runner.go` 顶部注释；步骤级期望包括
`expect_exception`（十进制异常码）、`expect_values`、`expect_txn`、
`expect_error`（`eof`/`timeout`/`refused`/`frame`）。

## 协议行为与边界

- **事务 ID**：仅在同一条 TCP 连接上用于请求/响应匹配，服务器原样回显（含 `0x0000`、重复值）。标准不要求、本实现也不做跨请求去重——因此 FC10 重试不是幂等操作，需要幂等的上层应用必须自行携带应用层去重键。
- **单元 ID**：仅服务 `-units` 白名单（默认 `1`，可配多个），其它单元返回 `0x0B`（网关目标无响应，Modbus TCP 对不可路由单元的惯例）。
- **非法 MBAP**：Length 小于 `单元ID+PDU(≥3)` 或 Protocol ID ≠ 0 时，不存在可信的事务 ID，不能回异常帧；服务器记录日志并关闭连接。
- **半包断连**：读到一半 EOF/RST 即丢弃该连接，未读满的请求不执行。
- **FC10 与 FC03 限制**：写 1..123 个寄存器、读 1..125 个（MODBUS V1.1b3 第 6 节）；字节计数必须等于 `2×数量`，否则异常 `0x03`。
- 所有数字与哈希均为真实运算；依赖版本在 `go.mod` / `go.sum` 中锁定。

## 回执链格式（v1）

```
prev[1]   = SHA256("modbus-receipt-genesis-v1")                 # 十六进制
body      = "v1|seq|txn|unit|addr|qty|valuesHex"                # valuesHex: 每寄存器 4 位小写 hex 拼接
bodyHash  = SHA256(body)
chainHash = SHA256(prev[i] ‖ bodyHash[i])
mac       = HMAC-SHA256(key, prev[i] ‖ bodyHash[i])
```

`receipts` 表：`seq, transaction_id, unit_id, address, quantity, values_hex, body_sha256, prev_sha256, chain_sha256, hmac_sha256, created_at`。
`exceptions` 表：`id, created_at, transaction_id, unit_id, function, exception_code, reason`。
数据库以 WAL 模式打开，可用 `sqlite3` CLI 或本程序并发只读。

## 排错

- 端口占用：换 `-listen 127.0.0.1:1503`，场景 JSON 顶部 `address` 同步修改。
- `CHAIN BROKEN`：有人改过 DB、用过不同密钥，或文件不是同一服务器产物。
- Windows/macOS 同为纯 Go，`go build ./...` 即可；验收脚本中的 `/dev/tcp` 探测需要 bash。
