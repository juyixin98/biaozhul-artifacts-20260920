# UDP 可靠传输模拟（Go / net/http，纯后端）

在不可靠链路上实现一个 **UDP 可靠文件传输子集**：32 位序号、累积 ACK、
滑动窗口、超时与快速重传、整体 SHA-256 校验，并显式定义**序号回绕比较**
与**连接代际（generation）**。链路可在「内存邮箱」与「真实 UDP 回环」
之间切换，故障（丢包 / 重复 / 乱序 / 旧连接幽灵报文）由同一层可注入，
随机源用固定种子、次数可用预算封顶。时间用**可注入时钟**，因此核心测试
完全确定性、不依赖真实 sleep。

> 仅后端，无界面。对外只提供一个 `net/http` 接口 `POST /transfer`。

---

## 1. 目录结构与依赖

```
.
├── go.mod                 模块定义；零第三方依赖（仅 Go 标准库）
├── packet.go              报文类型、二进制编解码、序号回绕比较、FIN/SYN 元数据
├── clock.go               Clock 接口 + RealClock / FakeClock（可注入时钟）
├── link.go                Link 抽象、故障注入器 Injector、内存链路 Pipe
├── udp.go                 真实 UDP 回环链路（net.UDPConn），复用同一故障层
├── session.go             发送方 / 接收方状态机、RunTransfer、统计结构
├── cmd/server/main.go     net/http 服务（POST /transfer）
├── cmd/server/main_test.go HTTP handler 测试
├── examples/requests.sh   curl 请求样例脚本
└── *_test.go              自动化测试
```

**依赖**：Go ≥ 1.23，仅标准库（`net/http`、`net`、`crypto/sha256`、
`math/rand`、`encoding/binary` 等）。无任何第三方模块，因此
`go.mod` 本身就是完整的依赖锁定；`go mod tidy` 后不产生 `go.sum`
（没有需要校验和的外部依赖）。

---

## 2. 启动命令

```bash
# 编译检查 / 运行测试
go vet ./...
go test ./...                 # 含 UDP 真实回环测试
go test -race ./...           # 竞态检测（推荐）
go test -short ./...          # 跳过真实 UDP 测试

# 启动 HTTP 服务（默认 :8080）
go run ./cmd/server
# 或指定端口
go run ./cmd/server -addr :18080
```

服务启动后：

```bash
# 查看接口说明
curl -s http://localhost:8080/

# 最常见调用：请求体即“文件”原始字节
curl -s -X POST --data-binary @somefile.bin \
  'http://localhost:8080/transfer?mode=fake&seed=42&loss=1&budget=10'
```

也可以一次跑全部样例：

```bash
BASE=http://localhost:8080 ./examples/requests.sh
```

---

## 3. HTTP 接口

### `POST /transfer`

请求体是被传输文件的**原始字节**（上限 16 MiB）。服务端在内部建立一对
链路端点（内存或真实 UDP 回环），让发送方把请求体可靠传给接收方，
接收方逐字节写出并计算整体哈希，然后返回 JSON 报告。

查询参数（全部可选）：

| 参数 | 默认 | 含义 |
| --- | --- | --- |
| `mode` | `fake` | `fake`=内存链路；`udp`=真实 UDP 回环 |
| `seed` | `1` | 故障随机种子，**固定种子可复现** |
| `loss` / `ackloss` | `0` | 数据 / ACK 方向丢包概率，范围 `[0,1]`，`1`=必丢 |
| `dup` / `ackdup` | `0` | 两个方向的报文重复概率 |
| `reorder` | `0` | 数据方向乱序概率 |
| `hold` | `2` | 乱序时扣留多少个后续报文再投递（同时有延迟上界） |
| `budget` | `20` | 各类故障的**总次数预算**：正数=最多 N 次（有限故障）；`0`=不限 |
| `mss` | `1024` | 数据块大小（字节） |
| `window` | `8` | 滑动窗口大小（报文数） |
| `rto` | `25ms` | 重传超时（Go duration，如 `50ms`） |
| `startseq` | `0` | 起始序号；用接近 `2^32` 的值可验证序号回绕 |
| `gen` | `42` | 本次连接代际；会注入 `gen-1` 的旧连接幽灵报文 |

返回 JSON 字段：

- `ok`：传输是否成功且接收字节与请求体逐字节一致；
- `hashes.sender` / `hashes.receiver`：双方流式计算的整体 SHA-256；
- `sender`：首次发送数、超时重传、快速重传、超时次数、重复 ACK 数、
  **窗口内存高水位**（`MaxBufferedPackets` / `MaxBufferedBytes`）等；
- `receiver`：按序上交数、乱序/重复丢弃数、忽略的旧代际报文数等；
- `faults.dataDirection` / `faults.ackDirection`：两个方向实际发生的
  丢包 / 重复 / 乱序 / 幽灵报文计数（用于核对故障确实被注入）。

### 请求样例

```bash
# 1) 干净传输
curl -s -X POST --data-binary @file.bin http://localhost:8080/transfer?mode=fake

# 2) 固定种子，四类故障全开但次数封顶（必然完成，验收同款场景）
curl -s -X POST --data-binary @file.bin \
  'http://localhost:8080/transfer?mode=fake&seed=20260924&loss=1&ackloss=1&dup=1&ackdup=1&reorder=1&hold=3&budget=12'

# 3) 真实 UDP 回环 + 随机故障
curl -s -X POST --data-binary @file.bin \
  'http://localhost:8080/transfer?mode=udp&seed=42&loss=0.2&ackloss=0.25&dup=0.05&reorder=0.1&budget=0'

# 4) 序号回绕
curl -s -X POST --data-binary @file.bin \
  'http://localhost:8080/transfer?mode=fake&mss=200&window=4&startseq=4294967290&loss=0.4'
```

---

## 4. 协议设计

### 4.1 报文

类型：`SYN / SYNACK / DATA / ACK / FIN / FINACK / ERROR`。
定长 21 字节头：`type(1) | seq(4) | ack(4) | gen(8) | payloadLen(4)`，
后接载荷（大端序）。

- `DATA`：`Seq` 为本块序号，载荷为最多 MSS 字节的文件数据；
- `ACK`：累积确认，`Ack` = 已连续收到的最后序号 + 1；
- `FIN`：`Seq` = 最后数据序号 + 1，载荷携带 `总长度(8) + 哈希长度(2) + SHA-256`；
- `SYN`：载荷携带提议的起始序号与 MSS；
- 每个报文都带 64 位 `Gen`（连接代际）。

### 4.2 连接生命周期与代际

SYN/SYNACK 建连 → 滑动窗口传数据 → FIN（带总长与整体哈希）/ FINACK，
最后发送方再回一个 FINACK 完成挥手；接收方在挥手等待期内对重传 FIN
持续重确认，等不到最终确认则按 `MaxRetries` 个 RTO 有界退出。

**连接代际**：每次连接用一个 `gen`。接收方只接受 `pkt.Gen == gen` 的
SYN 建连，建连后忽略任何代际不符的报文。故障层注入的 `gen-1`
SYN/DATA/FINACK（“旧连接幽灵报文”）因此无法污染当前连接，接收方把它们
计入 `StalePackets`。

### 4.3 序号与回绕比较

序号是 32 位无符号整数，算术在模 2³² 环上进行：

- `seqLess(a,b)`：`(b-a) mod 2³² ∈ (0, 2³¹)`，即环上从 a 正向不到
  半圈能走到 b；半圈整不算“小于”，避免环两端的歧义；
- `seqInWindow(base,x,n)`：`(x-base) mod 2³² < n`，判定报文/ACK
  是否落在当前窗口；
- 发送窗口用环形数组（长度 = WindowSize）保存未确认载荷，天然支持回绕。

### 4.4 滑动窗口与重传（Go-Back-N 风格）

- 发送方维护 `base`（最早未确认）与 `next`（下一个待发），窗口内未确认
  报文保存在**长度固定为 WindowSize 的环形缓冲**里，因此发送端缓冲
  **报文数恒 ≤ WindowSize、字节数恒 ≤ WindowSize×MSS**；
- 接收方**不缓存乱序报文**：只接受 `Seq == expected` 的 DATA，乱序超前
  或重复落后一律丢弃并立即回当前期望序号的 ACK（重复 ACK）。数据按序
  直接写入输出文件，接收端额外内存为 O(1)（不含一个 MSS 大小的读缓冲）；
- 累积 ACK 推动 `base` 前移并释放缓冲，然后继续填满窗口；
- **超时重传**：RTO 到期重传整个窗口（由注入时钟计时）；
- **快速重传**：收到第 3 个重复 ACK 时立即重传整个窗口。采用**边沿触发**
  （只有“第 3 个”触发一次），否则重传的整窗报文会各自再产生重复 ACK，
  形成重传雪崩（开发中实测过该正反馈，单次传输重传达 8 万+，修正后恢复正常）；
- 同一阶段连续超时超过 `MaxRetries` 即有界失败（`ErrMaxRetries`），
  不会无限挂死。

### 4.5 整体哈希

发送方边读边算 SHA-256（流式，不额外缓存整个文件），随 FIN 发出；
接收方边写边算，收到 FIN 后同时校验**总长度**与**整体哈希**，
不一致回 `ERROR("hash mismatch")` 并返回 `ErrHashMismatch`。
（本协议不做逐包校验和，模拟“数据可能被破坏但需端到端发现”，
测试中通过在入站链路篡改一个 DATA 载荷来验证。）

### 4.6 可注入时钟

协议只通过 `Clock` 接口取时间、建定时器。`RealClock` 包装标准库；
`FakeClock` 提供手工推进的虚拟时钟与 `Step(d)`（有界小步进）。
核心测试用 FakeClock 驱动 RTO/挥手定时器，确定性地制造“超时”，
不需要真实等待。

### 4.7 故障层

`Injector` 装饰在任意单向链路上（内存 Pipe 与真实 UDP 共用同一份代码），
按固定种子的 `rand.Source` 独立决定每个报文：丢包 / 重复 / 乱序扣留，
并可在前若干个报文前穿插旧代际幽灵报文。每类故障支持概率 + **次数预算**
（`-1` 不限、`0` 关闭、正数封顶）。乱序扣留除“等 N 个后续报文”外还有
**延迟上界**（由注入时钟计时的一次性冲刷定时器），保证连接尾部的
FIN/FINACK 被扣留后也会在一个上界内释放，不依赖 Close。

---

## 5. 验收点与对应测试

| 验收要求 | 测试 |
| --- | --- |
| 固定种子注入丢包、重复、乱序、旧连接报文，有限故障下传输完成 | `TestAcceptanceFiniteFaults`（断言四类故障计数>0、预算精确耗尽、内容与哈希一致、旧代际全部被忽略） |
| 序号 + ACK + 滑动窗口 + 重传 + 整体哈希 | `TestTransferClean`、`TestAcceptanceStochastic`、`TestHashMismatchDetected` |
| 序号回绕比较 / 跨 2³² 传输 | `TestSeqComparison`、`TestSeqInWindow`、`TestSequenceWraparound` |
| 连接代际隔离 | `TestGenerationIsolation`（代际不匹配时无法建连且有界失败）、各用例中的幽灵报文断言 |
| 窗口内存有上限 | `TestWindowMemoryBounded`（高水位 ≤ WindowSize 包 / WindowSize×MSS 字节） |
| 无限故障时有界失败、不挂死 | `TestUnboundedLossFailsBounded` |
| 多种子鲁棒性 | `TestManySeedsStress`（40 个种子全部完成） |
| 真实 UDP 回环 | `TestUDPLoopback`、`TestUDPLoopbackClean`（真实时钟 + 真实丢包） |
| HTTP 接口 | `cmd/server` 的 `TestTransferHandler` |

---

## 6. 实际运行结果

以下为在本机（Linux x86_64，Go 1.23.4）的真实运行记录。

### 6.1 自动化测试

```text
$ go test -race -timeout 240s -count=1 ./...
ok  	reliableudp             ~22s
ok  	reliableudp/cmd/server  ~1.3s
```

连续 5 轮 `-race` 全部通过；UDP 真实回环测试 `-count=3` 全部通过。
`-short` 模式跳过真实 UDP 测试后约 0.6s。

真实 UDP 回环（8000 字节，数据方向丢包率 0.15、ACK 方向 0.2）的
`-v` 日志示例：

```text
UDP loopback: 8000 bytes in 123ms, DATA lost(AB)=9 ACK lost(BA)=7,
retransmits=41 (timeout=9 fast=32), stale=3
--- PASS: TestUDPLoopback
```

### 6.2 HTTP 端到端样例（真实抓取，经 examples/requests.sh）

- **空文件**：`ok:true, size:0`；
- **固定种子四类故障（预算 12）**：
  `ok:true, elapsed≈467ms, hash 相等`；
  DATA 方向 lost/dup/reorder/ghost = 12/12/12/3，
  ACK 方向 = 12/12/0/3（该种子下 ACK 方向乱序预算被概率/时序消耗为 0 次抽中，属正常）；
  快速重传 47 次；窗口高水位 8 包 / 8192 字节；接收端忽略旧代际报文 3 条；
- **真实 UDP 回环 + 随机故障**：`ok:true, elapsed≈84ms, hash 相等`；
- **序号回绕 + 1 MiB**：`ok:true`，`maxBufferedPackets=16 (≤16)`、
  `maxBufferedBytes=16384 (≤16384)`，哈希相等。

> 注：内存 `fake` 模式默认也使用真实时钟（HTTP 服务场景），因此耗时是
> 真实墙钟时间；库的单元测试用 FakeClock，故很快且确定。

---

## 7. 设计取舍与说明

- **Go-Back-N 而非选择重传**：题目要求的“子集”内，GBN 实现简单且能完整
  展示序号/ACK/窗口/重传；代价是一个报文丢失会重传整个窗口。接收端因此
  不需要乱序重组缓冲，窗口内存上限的论证也最简单。
- **接收方不做数据持久化分块**：直接写传入的 `io.Writer`，可接文件、
  `bytes.Buffer` 或 `io.Discard`。
- **乱序注入必须有延迟上界**：仅靠“等 N 个后续报文”释放会在连接尾部
  （FIN/FINACK 之后基本无新报文）永久扣留报文——这是开发中通过一个
  `-race` 下偶发挂死定位并修复的真实活性问题，现由注入时钟驱动的
  冲刷定时器统一兜底（内存链路与 UDP 链路同一套机制）。
- **FIN 确认只认真正的 FINACK**：ACK 方向可能滞留数据阶段的旧累积 ACK
  （经乱序延迟冲刷），其确认号恰为 finSeq，不能据此判定 FIN 已确认。
- **故障“不限预算 + 概率<1”在无限时间意义上仍会收敛**，但 HTTP 与
  测试都有 `MaxRetries` / 超时兜底；要严格保证“必然完成”，请用
  正数 `budget`（有限故障）——这正是验收场景的构造方式。

---

## 8. 未完成项 / 已知边界（如实记录）

- 只实现了**单工**的文件传输（一个发送方、一个接收方）；没有双向复用、
  拥塞控制（cwnd/慢启动）、SACK 选择确认、RTT 估计与动态 RTO；RTO 固定。
- 没有逐包校验和（只做端到端整体 SHA-256），真实 UDP 自带的 16 位
  校验和未额外扩展。
- `fake` 模式是同一进程内的内存链路，不经过内核网络栈；要走真实网络
  请用 `mode=udp`（当前固定为本机回环 127.0.0.1 两个临时端口）。
- HTTP 服务对每次请求新建一对端点、单连接串行传输，没有做并发会话池、
  鉴权与断点续传；请求体上限 16 MiB。
- 无限故障（`budget=0` 且概率较高）在极端时序下可能触发 `MaxRetries`
  返回失败——这是有意的有界保护，不是挂死；严格的“必然完成”保证只
  针对有限故障（正数预算）成立。
