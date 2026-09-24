# Modbus 写入回执（Modbus Write Receipts）

一个从零实现的纯后端 Modbus/TCP 网关 + 回放客户端：

- **Go** 单仓库，标准库实现 Modbus/TCP 协议；
- **SQLite**（纯 Go 驱动 `modernc.org/sqlite`，无需 CGO）持久化保持寄存器与**写回执**；
- 明确支持的功能码只有两个：
  - `0x03` Read Holding Registers（读保持寄存器）
  - `0x10` Write Multiple Registers（写多个寄存器）
- 校验 MBAP 头（长度、协议 ID、事务 ID 回显）、单元 ID 白名单、寄存器地址范围；
- TCP 分片（分包）与合包（粘包）对上层透明；
- 整批写入原子化：任一寄存器越界，**整次写失败**；并发读永远看不到"半批"数据；
- 每次成功写入生成一条带 **SHA-256 哈希链**的密码学回执，可离线验签、可检测篡改。

> 诚实的协议边界：Modbus/TCP 标准本身**不提供**跨连接幂等性、去重或"响应丢失自动回滚"。
> 本项目不虚构这些能力——详见下文 [协议行为，如实说明](#协议行为如实说明)。

---

## 目录结构

```
.
├── cmd/
│   ├── modbusd/        # 服务器：Modbus/TCP 网关 + SQLite
│   └── modbusctl/      # 客户端：read / write / replay / receipts / verify
├── internal/
│   ├── protocol/       # MBAP 帧、流式分帧、PDU 编解码、异常码
│   ├── storage/        # SQLite 寄存器库 + 原子批量写 + 哈希链回执
│   ├── server/         # TCP 连接处理、校验与分发
│   ├── client/         # 客户端库（事务 ID 校验、异常码解析）
│   └── replay/         # JSONL 场景文件解析与执行
├── examples/demo.jsonl # 示例输入：覆盖异常码/粘包/重复事务ID/中途断连
├── go.mod / go.sum     # 锁定依赖
└── README.md
```

## 环境要求

- Go ≥ 1.22
- 无 CGO 依赖（`modernc.org/sqlite` 是纯 Go），`CGO_ENABLED=0` 也可构建。

## 快速开始（本地启动）

```bash
# 1. 下载锁定依赖（go.sum 已提交）
go mod download

# 2. 构建
go build ./...

# 3. 启动服务器（默认监听 :1502，32 个寄存器，单元 ID=1）
go run ./cmd/modbusd -listen 127.0.0.1:1502 -bank 32
```

另开一个终端：

```bash
# 写一批：地址 0 起写 4 个寄存器 10/20/30/40（事务 ID 自动分配）
go run ./cmd/modbusctl write -a 127.0.0.1:1502 0 10 20 30 40

# 读回
go run ./cmd/modbusctl read  -a 127.0.0.1:1502 0 4

# 跑示例场景（异常码、粘包、分包、重复事务 ID、中途断连全覆盖）
go run ./cmd/modbusctl replay -a 127.0.0.1:1502 examples/demo.jsonl

# 查看写回执（哈希链）
go run ./cmd/modbusctl receipts -db data/modbus.db

# 离线校验哈希链（篡改检测）；退出码 0=完好，3=被篡改
go run ./cmd/modbusctl verify -db data/modbus.db
```

## 验收命令（一键）

```bash
# 自动化测试：协议层 / 存储层 / 真实 TCP 端到端 / 示例场景
go test -race -count=1 ./...

# 手动端到端验收（在两个终端）
go run ./cmd/modbusd -listen 127.0.0.1:1502 -bank 32
go run ./cmd/modbusctl replay -a 127.0.0.1:1502 examples/demo.jsonl
# 期望结尾：N step(s), 0 failure(s)，且每一步都标记 ok
go run ./cmd/modbusctl verify -db data/modbus.db
# 期望：OK: receipt hash chain intact (...)
```

也可以用 Make 风格的简单脚本：

```bash
go run ./cmd/modbusd -listen 127.0.0.1:1502 -bank 32 & SRV=$!
sleep 1
go run ./cmd/modbusctl replay -a 127.0.0.1:1502 examples/demo.jsonl
go run ./cmd/modbusctl verify -db data/modbus.db
kill $SRV
```

## 命令行参考

### `modbusd` 服务器

| 参数 | 默认 | 说明 |
|---|---|---|
| `-listen` | `:1502` | 监听地址（标准 Modbus/TCP 端口 502 需要 root，故默认 1502） |
| `-db` | `data/modbus.db` | SQLite 文件（自动建库建表，WAL 模式） |
| `-bank` | `1000` | 保持寄存器数量，合法地址 `0..bank-1` |
| `-units` | `1` | 接受的单元 ID 白名单，逗号分隔，如 `1,2` |
| `-idle-timeout` | `0`（永不） | 空闲连接超时，如 `30s` |

### `modbusctl` 客户端

```
modbusctl read     -a HOST:PORT [-u UNIT] [-t TXN] ADDRESS QUANTITY
modbusctl write    -a HOST:PORT [-u UNIT] [-t TXN] ADDRESS V1 [V2 ...]
modbusctl replay   -a HOST:PORT [-json] SCENARIO.jsonl
modbusctl receipts -db PATH [-limit N] [-json]
modbusctl verify   -db PATH
```

- 数值支持十进制（`1234`）、十六进制（`0x04D2`）、二进制（`0b1001`）。
- `-t 0` 自动分配事务 ID；显式给任何值都会原样发送（**重复事务 ID 合法且不会去重**）。

## 回放场景文件（JSONL）

每行一个 JSON 对象，`#` 开头为注释。操作类型：

| op | 字段 | 含义 |
|---|---|---|
| `write` | `txn,unit,start,values` | FC10 写一批 |
| `read` | `txn,unit,start,qty` | FC03 读一批 |
| `raw` | `bytes`（hex）, `expect_response` | 发送任意原始字节（制造粘包/分包/畸形帧） |
| `recv` | `txn`（可选） | 读取一个 ADU；给 `txn` 则校验事务 ID |
| `connect` / `disconnect` | — | 建立/关闭 TCP 连接（模拟中途断连） |
| `sleep` | `ms` | 等待 |

任何步骤可带 `"expect_error":"0x02"`：仅当返回错误/异常包含该子串时该步骤才算通过。
`examples/demo.jsonl` 用它断言越界写返回 `0x02`、未知单元返回 `0x0B`、畸形 PDU 返回 `0x03`。

## 协议实现细节

### MBAP 头校验（7 字节）

```
TransactionID(2) ProtocolID(2) Length(2) UnitID(1) | PDU(...)
```

- **Length**：必须 `≥1` 且 `≤254`（单元 ID 1 字节 + PDU 至多 253 字节）；声明多少就读多少，`io.ReadFull` 负责跨 TCP 段重组，因此**分包不影响解析**；连续帧按 Length 切分，因此**粘包不会串帧**。
- **ProtocolID**：必须为 `0x0000`，否则返回异常 `0x03`。
- **TransactionID**：服务器**原样回显**；客户端库严格校验回显是否匹配，不匹配报 `ErrTransactionMismatch`。重复值完全合法（见下）。
- **UnitID**：命中白名单（默认 `{1}`）才处理；未知单元返回 `0x0B Gateway Path Unavailable`（模拟串行网关找不到从站）。

### 功能码与异常码

| 情形 | 响应 |
|---|---|
| 正常 FC03 | `0x03` + 字节数 + 寄存器值（大端） |
| 正常 FC10 | `0x10` + 回显起始地址 + 回显数量 |
| 未知功能码 | `0x01 Illegal Function` |
| 地址越界（读或写） | `0x02 Illegal Data Address` |
| 数量非法 / 字节数不符 / PDU 长度不符 | `0x03 Illegal Data Value` |
| 未知单元 ID | `0x0B Gateway Path Unavailable` |
| 存储层内部错误 | `0x04 Server Device Failure` |

数量上限遵循标准：FC03 一次 1..125，FC10 一次 1..123（受 PDU 253 字节上限约束）。

### 原子批量写与并发可见性

`Store.Write` 一次完成：**先做整窗地址范围检查 → 在单个 SQLite 事务内 UPSERT 全部寄存器 → 插入回执 → 提交**。

- 任一寄存器越界：事务根本不开启，**整批零改动**，返回 `0x02`；
- 事务内任何语句失败：回滚，整批零改动；
- 并发控制：`sync.RWMutex` + 单连接 SQLite（WAL）。写持写锁，读持读锁且各自是只读事务快照，因此读者只会看到**提交前**或**提交后**的完整状态，绝不可能读到半个批次。`internal/storage` 与 `internal/server` 均有并发测试证明这一点（`-race` 下运行）。

### 写回执与哈希链（密码操作真实执行）

每次成功的 FC10 在**同一事务**内写入一行回执：

| 列 | 内容 |
|---|---|
| `seq` | 自增序号 |
| `transaction_id` / `unit_id` | 请求 MBAP 字段 |
| `start_address` / `quantity` | 批地址与数量 |
| `value_blob` | 寄存器值，大端拼接 |
| `client_addr` | 客户端 `host:port` |
| `committed_at` | RFC3339Nano UTC 时间戳 |
| `prev_hash` / `hash` | SHA-256 哈希链 |

哈希定义（第三方可独立复算）：

```
hash_n = SHA256( hex(prev_hash_{n-1})
               || committedAt(RFC3339Nano UTC)
               || TransactionID u16be || UnitID u8
               || StartAddress u16be || Quantity u16be
               || value_blob(大端)
               || clientAddr(UTF-8) )
hash_0 的 prev 为空串 ""
```

`modbusctl verify` 从库中逐行重算并比对，任一历史行被篡改都会断链并报告首个断裂序号（退出码 3）。
测试用例直接 `UPDATE receipts` 篡改数据验证检测有效。

## 协议行为，如实说明

以下是**标准协议真实具有 / 不具有**的语义，本实现严格照做，不夸大：

1. **事务 ID 不是去重键。** Modbus/TCP 用它匹配请求/响应，值可由客户端任意选择、重复。
   服务器对相同 TransactionID 的两次 FC10 会**执行两次**、生成两条回执（有专门测试）。
   标准协议**没有**跨连接（甚至同连接）的幂等性承诺；本项目也不假装有。
2. **响应丢失 ≠ 写入回滚。** 客户端在 FC10 被服务器处理后、读到响应前断开（或网络丢包），
   按 Modbus/TCP 语义该写入**已经生效**；重连后读得到新值，服务器不会自动撤销，也不会凭旧
   TransactionID 跳过重发。需要幂等的业务应在应用层设计，不应归咎于 Modbus。
3. **帧中途断连。** 若对端在一个 ADU 中间断开，服务器无法安全再同步（剩余字节长度只有发送方知道），
   按标准直接关闭该连接；该帧因不完整而**不会**执行（有测试：发送半个 FC10 后断开，寄存器不变）。
4. **TCP 保证字节流有序、不丢不重**，因此同一连接上的请求/响应次序可靠；跨连接之间没有顺序约定。
5. 单元 ID 在纯 TCP 网关中通常被忽略，但本实现选择**显式白名单**以满足"校验单元 ID"的要求，
   这是一种有据可依的网关策略（非透传模式），不是标准强制行为。

## 测试覆盖

`go test -race ./...` 包含：

- **协议层**：FC03/FC10 解析、异常码编码、数量边界、Length 越界；
  真实 TCP socket 上逐字节发送（分包）、双帧合包（粘包）、任意切分点、帧截断（`ErrTruncated`）；
- **存储层**：越界整批失败且无前缀落地、读越界、哈希链正确性、篡改检测、失败批次无回执、
  并发读者在写未提交时阻塞（证明无半批可见）；
- **服务器端到端（真实 TCP）**：正常写读覆盖、`0x01/0x02/0x03/0x0B` 异常、
  错误 ProtocolID、事务 ID 原样回显、**重复事务 ID 两次都执行**、
  请求后断连（写仍生效）、**帧中途断连（无副作用）**、粘包双写、逐字节分包、
  8 写者 × 4 读者高并发无撕裂读；
- **回放**：`examples/demo.jsonl` 全场景必须 0 失败；`expect_error` 断言的正/反例。

## 依赖（已锁定）

- `modernc.org/sqlite v1.34.1`（纯 Go SQLite；及 `modernc.org/{mathutil,memory,strutil,token}`）
- 其余仅用 Go 标准库。

`go.sum` 已提交；`go mod verify` 可校验缓存完整性。
