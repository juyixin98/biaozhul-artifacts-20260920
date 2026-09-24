# UDP 可靠传输模拟器（udpreliable）

纯后端 Go 项目：在回环链路上模拟一个可靠的 UDP 文件传输协议子集，并通过
HTTP 接口触发传输、返回统计。无界面，无第三方依赖（仅标准库）。

## 功能

- **序号与回绕比较**：32 位序号，RFC 1982 串行号比较（`int32(a-b) < 0`），
  支持序号空间跨越 2³² 回绕点（`internal/proto/packet.go`）。
- **连接代际（generation）**：每条连接有随机 32 位代际 ID，所有报文携带；
  代际不匹配的报文（旧连接残留）一律丢弃并计数。
- **累积 ACK**：接收方对每个 Data 报文回复"下一个期望序号"的累积 ACK。
- **滑动窗口**：发送方在途报文数 ≤ window；接收方乱序缓存 ≤ window。
  窗口即内存上界：两侧各不超过 `window × (chunkSize + 16)` 字节的报文缓存。
- **超时重传**：发送方为最老未确认报文维护 RTO 定时器，超时重传全部在途报文。
- **整体哈希校验**：数据全部确认后，发送方发送携带全文件 SHA-256 的 FIN；
  接收方重组完毕后比对哈希，回 DONE(ok/fail)；发送方以 DoneAck 收尾。
- **可注入时钟**：`clock.Clock` 接口，生产用真实时钟，测试用手动时钟
  （`internal/clock`），重传行为可确定性验证。
- **可注入丢包层**：`sim.Lossy` 以固定种子确定性地注入丢包、重复、乱序
  （扣押-后放）故障；种子相同则故障序列完全相同。
- **两种回环链路**：`mem`（内存 channel，完全确定）与 `udp`
  （127.0.0.1 真实 UDP socket）。

## 依赖与构建

- Go ≥ 1.23（开发验证版本：go1.23.4 linux/amd64）
- 无第三方模块，`go.mod` 即完整依赖锁定（零外部依赖，无需 go.sum）

```sh
go build ./...        # 编译
go test ./...         # 运行全部自动化测试
go test ./... -race   # 竞态检测
```

## 启动

```sh
go run ./cmd/udpsim -addr :8080
# 或
go build -o udpsim ./cmd/udpsim && ./udpsim -addr :8080
```

## HTTP 接口

- `GET /api/health` — 存活探针
- `GET /` — 帮助文本
- `POST /api/transfers` — 同步执行一次模拟传输，请求体 JSON：

| 字段 | 默认 | 说明 |
|---|---|---|
| `sizeBytes` | 0 | 文件大小（0–256MiB），内容由 seed 确定性生成 |
| `seed` | 0 | 同时种子化文件内容、连接代际与故障序列 |
| `dropRate` | 0 | 每报文丢弃概率（每个方向独立） |
| `dupRate` | 0 | 每报文重复概率 |
| `reorderRate` | 0 | 每报文乱序概率 |
| `stalePackets` | 0 | 向接收方注入的旧代际报文数 |
| `window` | 16 | 滑动窗口（报文数，1–4096） |
| `chunkSize` | 1024 | 每报文载荷字节（64–60000） |
| `rtoMs` | 50 | 重传超时（毫秒） |
| `timeoutMs` | 30000 | 整体墙钟超时（毫秒） |
| `transport` | `mem` | `mem` 内存链路 / `udp` 回环 UDP |
| `initialSeq` | 0 | 起始序号，设为接近 2³² 可验证回绕 |

约束：`dropRate + dupRate + reorderRate ≤ 0.95`（有限丢包前提，保证重传
以概率 1 终止）；违反时返回 400。

### 请求样例

```sh
# 回环 UDP，10% 丢包 + 5% 重复 + 10% 乱序 + 5 个旧连接报文
curl -s localhost:8080/api/transfers -d '{
  "sizeBytes": 262144, "seed": 42,
  "dropRate": 0.10, "dupRate": 0.05, "reorderRate": 0.10,
  "stalePackets": 5, "window": 16, "chunkSize": 1024,
  "rtoMs": 20, "transport": "udp"
}'

# 序号回绕：起始序号 2^32 - 16
curl -s localhost:8080/api/transfers -d '{
  "sizeBytes": 65536, "seed": 7, "dropRate": 0.05,
  "initialSeq": 4294967280, "rtoMs": 20
}'
```

也可直接执行 `examples/requests.sh`（需服务已启动）。

### 响应样例（实测，上面第一个请求的真实输出）

```json
{
  "ok": true,
  "bytes": 262144,
  "sha256": "6423533767955274a95c7b1ee3e460c2a87e67f58d57afa1663dd3969373f050",
  "hashMatch": true,
  "durationMs": 291,
  "sender":   {"dataSent": 467, "retransmits": 211, "finSent": 3, "acksRecv": 416, "staleDrop": 0, "maxInFlight": 16},
  "receiver": {"dataRecv": 442, "dupRecv": 186, "acksSent": 442, "finRecv": 3, "doneSent": 3, "staleDrop": 5, "maxBuf": 16},
  "dataFaults": {"sent": 472, "dropped": 48, "duplicated": 23, "reordered": 41},
  "ackFaults":  {"sent": 445, "dropped": 42, "duplicated": 18, "reordered": 44}
}
```

`maxInFlight` 与 `maxBuf` 分别为发送方在途报文、接收方乱序缓存的
高水位，均不超过 `window`（本例 16）——窗口内存上界的直接证据。
`receiver.staleDrop = 5` 即注入的 5 个旧代际报文全部被丢弃。

## 协议格式

固定 16 字节头 + 载荷：

```
0:2   magic 0x5250
2:6   generation（连接代际）
6     类型：1=Data 2=Ack 3=Fin 4=Done 5=DoneAck
7     标志：Done 的 bit0 = 哈希校验通过
8:12  序号（Data=块序号；Ack=累积下一期望；Fin=InitialSeq+总块数）
12:16 保留
```

Fin 载荷为全文件 SHA-256（32 字节）。

## 目录结构

```
cmd/udpsim/            HTTP 服务入口
internal/proto/        报文编解码、序号回绕比较
internal/clock/        可注入时钟（真实 / 手动）
internal/transport/    滑动窗口发送方、接收方（核心协议）
internal/sim/          丢包注入层、mem/udp 回环链路、传输编排
internal/httpapi/      HTTP 接口
examples/requests.sh   curl 请求样例
```

## 测试与实测结果

测试覆盖：

- `proto`：编解码往返、坏包拒绝、序号回绕比较表（含 2³² 边界两侧）
- `clock`：手动时钟定时器触发/停止
- `transport`：手动时钟驱动的超时重传（不重发不前进）、旧代际报文丢弃
- `sim`（验收主测试）：固定种子注入丢包/重复/乱序/旧连接报文，
  256KiB 传输完成且哈希一致；断言 `MaxInFlight ≤ window`、
  `MaxBuf ≤ window`；序号回绕传输；空文件；真实回环 UDP；
  同种子两次运行统计完全一致（确定性）；拒绝超限丢包配置
- `httpapi`：端到端 POST 传输、非法配置 400、坏 JSON 400、健康检查

本机实测（go1.23.4 linux/amd64，2026-09-24）：

```
$ go test ./... -count=1
ok  udpreliable/internal/clock      0.002s
ok  udpreliable/internal/httpapi    0.060s
ok  udpreliable/internal/proto      0.002s
ok  udpreliable/internal/sim        0.436s
ok  udpreliable/internal/transport  0.002s

$ go test ./... -count=1 -race   # 全部通过，无数据竞态
```

验收主测试实测统计（seed=42，256KiB，drop=0.10/dup=0.05/reorder=0.10，
stale=5，window=16）：传输完成、哈希一致；数据方向丢 48 / 重 23 / 乱 41，
ACK 方向丢 42 / 重 18 / 乱 44；重传 211 次；`MaxInFlight=16`、`MaxBuf=16`
均恰好不超窗口；5 个旧代际报文全部丢弃。

## 已知限制 / 未完成项

- 重传策略为"超时重传全部在途报文"，未实现快速重传（重复 ACK 触发）
  与 SACK；RTO 为固定值，未做 RTT 自适应（Karn/ Jacobson）。
- 单方向传输、单连接；无并发连接管理与握手协商（代际由种子派生而非协商）。
- 文件整体在内存中生成与重组（模拟定位），未做磁盘流式读写。
- 接收方完成后的 FIN 宽限期固定为 10×RTO；极端情况下发送方可能在
  宽限期外重传 FIN 而得不到回应（此时发送方在 MaxFinTries 后报错，
  由整体超时兜底）。
- HTTP 层每次请求同步执行一次传输，无异步任务队列与结果持久化。
