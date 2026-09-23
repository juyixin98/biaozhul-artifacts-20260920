# tsblock — 时间序列块编码（纯后端）

零第三方依赖（只用 Rust `std`）的文件型整数时间序列存储，外加一个本地
HTTP 验证入口。时间戳与值均为有符号 `i64`。

* 时间戳：**delta-of-delta（DOD）** 变长编码
* 值：**一阶差分（first difference）** + zigzag 变长编码
* 每点 1 bit 标签选择“紧凑差分”或“64 bit 原值逃逸”，溢出语义明确
* 块索引按时间范围做区间裁剪，查询只读重叠块
* 乱序点：整批拒绝（默认）或进入独立侧缓冲（查询不可见），显式 `drain` 压缩重写合并
* 存储 I/O 走可注入接口层，可模拟 **ENOSPC 部分写**与 **fsync 返回 EIO**
* 每条记录 CRC-32 校验；重开时尾部残记录被检出并截断

## 构建 / 测试

```bash
cargo build --release
cargo test                 # 32 个测试：单元 + 故障注入 + HTTP 端到端验收
cargo clippy --all-targets # 0 warning
```

无外部 crate，可离线构建（Rust 1.98 验证通过）。

## 运行

```bash
./target/release/tsblock --addr 127.0.0.1:8080 --data-dir ./data --block-size 1000 [--fault]
```

* `--addr` 默认 `127.0.0.1:0`（内核分配临时端口，启动日志打印实际端口）
* `--fault` 开启 `/dev/fault` 故障注入端点
* 请求样例：[`requests.http.md`](requests.http.md)（curl 清单）、
  [`requests.sh`](requests.sh)（自启动服务并跑完整个流程的可执行脚本）

## 磁盘格式

一个序列 = 数据目录下一个 `<name>.tsb` 文件。序列名白名单
（`[A-Za-z0-9_.-]`，不得以 `.`/`-` 开头），直接映射文件名且拒绝路径穿越。

```text
文件头（20 字节）
  0..4   magic   = b"TSBK"
  4..6   version = u16 LE = 1
  6..8   flags   = u16 LE；bit0 = 乱序策略（0 reject / 1 buffer）
  8..12  block_size = u32 LE
  12..20 reserved（8 个 0）

其后为顺序追加的记录
  payload_len : u32 LE
  crc32       : u32 LE   CRC-32/ISO-HDLC，仅对 payload 计算
  payload     : payload_len 字节（块编码，见下）
```

块索引**不单独持久化**：打开文件时逐条记录顺序扫描重建
（偏移、payload 长度、首末时间戳、点数）。扫描遇到短头、长度越界、CRC
不符或解码失败即判定为尾部残记录（崩溃 / ENOSPC 中途写的典型痕迹），
在该处截断并 `fsync`，此前所有完整记录保留。

### 块 payload（位流，MSB-first）

```text
count      : u32 LE（4 字节，字节对齐）
first_ts   : 64 bit
first_value: 64 bit
之后每个点：
  时间戳标签 0 : zigzag-varint
                 · 常规状态 = 与上一个时间差的 delta-of-delta
                 · 逃逸后首点 = 与上一点的原始 delta
  时间戳标签 1 : 64 bit 原始时间戳，并重置 DOD 预测器（溢出逃逸）
  值标签   0 : zigzag-varint 的一阶差分
  值标签   1 : 64 bit 原始值（溢出逃逸）
```

zigzag 后的无符号数用**位级** unsigned LEB128（每块 1 bit 续位 + 7 bit
载荷），因此标签和 varint 之间没有字节填充。

### 溢出语义（明确边界）

所有预测器运算用 `i128`。当 DOD（或重置后的原始 delta、或值差分）
不能放进 `i64` 时，编码器写标签 1 + 64 bit 原值并重置对应预测器；
解码器用同样的 `i128` 重建，任何越界结果报错。特别地，运行中的时间差
本身以 `i128` 保存——即使原始 delta 超过 `i64`，DOD 仍可能很小
（例：时间戳 `i64::MIN, -1, i64::MAX`，DOD = 1）。因此任意合法
`i64` 点序列都能无损往返；`i64::MIN`/`i64::MAX` 是合法端点。

## 同步边界（durability）

* 块记录 = **写完整条记录 + `fsync` 成功**后才进入内存索引、才算已持久化。
  二者任一失败：错误返回给客户端，点保留在活动块可重试，索引不更新；
  磁盘上可能出现的残记录由下次打开时 CRC 检出截断。
* 创建序列：写文件头 → 文件 `fsync` → 目录 `fsync`（保证目录项持久）。
* `drain` 压缩重写：写临时文件 `.name.compact.tmp` 并 `fsync` →
  原子 `rename` 覆盖 → 目录 `fsync`。失败则原文件不动。
* 活动块与侧缓冲仅在内存中：进程崩溃即丢失；缓冲点在 `drain` 成功前不持久。

## 乱序语义

迟到 = `ts <` 序列高水位（**相等时间戳是合法重复点，不拒绝**）。

* `reject`（默认）：写入前整批校验，批次自身必须时间非递减，且不得低于
  高水位；违规则整批拒绝（HTTP 409），不接受任何一个点。
* `buffer`：迟到点进独立侧缓冲，不推进高水位，对所有查询不可见；
  同批中的有序点照常接收。`POST /series/{n}/drain` 读出全部已落盘块 +
  活动块 + 侧缓冲，按时间戳**稳定排序**（历史点在并列时优先），重编码为
  block_size 块并原子重写整个文件。

## HTTP 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 存活检查 |
| GET | `/series` | 序列列表 |
| POST | `/series/{name}?policy=reject\|buffer&block_size=N` | 创建序列 |
| GET | `/series/{name}/stats` | 块数、磁盘字节、真实压缩比 |
| POST | `/series/{name}/points?sync=true` | 追加，body 每行 `ts,value` |
| GET | `/series/{name}/range?start=&end=` | 闭区间查询 |
| POST | `/series/{name}/flush` | 活动块落盘（写+fsync） |
| POST | `/series/{name}/drain` | 侧缓冲压缩重写合并 |
| GET | `/series/{name}/buffered` | 侧缓冲点数 |
| POST | `/dev/fault` | 仅 `--fault`：`write_limit=<字节>` / `fail_syncs=<n>` / `clear` |

查询响应含 `blocks_scanned`（为本次查询实际从磁盘读取并 CRC 校验的块数），
`bytes_read`，可直接观察索引区间裁剪效果。压缩比定义为
`(已落盘点 + 活动块点) × 16 ÷ 实际磁盘记录字节`，stats 实时给出。

## 实测结果（如实记录）

命令：`cargo build --release && bash requests.sh`（数据：10000 点，时间戳
间隔 58–64 秒并每 1000 点一次重复时间戳，值为 ±3 随机游走；block_size=1000）

```
写入：{"accepted":10000,"buffered":0,"blocks_flushed":10}
统计：disk_bytes=22763  uncompressed_bytes=160000  compression_ratio=7.02895
首点边界查询：blocks_scanned=1
全域查询：  count=10000 blocks_scanned=10
乱序写入：  HTTP 409 out-of-order point ts=1 is older than last accepted ts=9999999999
buffer：   2 个迟到点缓冲 → drain merged=2, blocks_rewritten=11
ENOSPC：   write_limit 后写入 → HTTP 500 "os error 28"；clear 后 flush 成功
重启：     10 个块全部恢复，数据一致
```

说明：7.03x 是这组“近等间隔 + 小幅游走”数据的实测值，不是格式承诺；
随机/剧烈跳变数据压缩比会接近 1（每点走 64 bit 逃逸）。未压缩基准统一为
`i64 ts + i64 value = 16 字节/点`。

边界 / 负值 / 重复时间戳 / `i64` 极值由自动化测试核验（对照朴素未压缩数组
逐项 `retain` 过滤）：

```
cargo test
# 26 个库内单元测试（位打包、varint、CRC 向量、编解码、溢出逃逸、
#   拒绝/缓冲语义、drain 压缩重写、索引裁剪……）
#  4 个故障注入集成测试（ENOSPC 部分写、人造残记录截断、fsync EIO、极值往返）
#  2 个 HTTP 端到端测试（5000 点对照参考数组的 8 组边界窗口、
#   压缩比一致性、409、缓冲/drain、故障重试、各类 400/404）
```

### 已知限制（非目标）

* 无前端；HTTP/1.1 为每连接单请求、无 keep-alive，仅供本地验证。
* 单写者锁（`Mutex`）；查询会读取活动块快照，面向功能验证而非吞吐基准。
* 块索引仅在内存；超大文件重开需顺序扫描全部记录头。
* 缓冲点仅在内存，drain 前不持久。
* 时间戳相等的重复点保留全部点（不去重）。
