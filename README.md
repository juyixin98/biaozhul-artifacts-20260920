# tsblock — 时间序列块编码（纯后端）

Rust 实现的文件支持时间序列存储库：整数时间戳 + 整数值的块编码
（delta-of-delta 时间、整数差分值）、块索引区间读取、可注入 I/O 层故障模拟、
本地 HTTP 验证入口。**零外部依赖**（仅用标准库），无前端。

## 构建与运行

```bash
cargo build --release
cargo test                      # 全部自动化测试（27 个）
cargo test --test acceptance -- --nocapture   # 验收测试，打印真实压缩比

# 启动本地 HTTP 验证入口
./target/release/tsblock --dir /tmp/tsblock-data --addr 127.0.0.1:8080
# 可选：--buffer-late（乱序点进入单独缓冲而非拒绝）  --block-size N（默认 256）
```

## 磁盘格式

每个 series 两个文件（series 名限制为 `[A-Za-z0-9_-]`，直接作为文件名）：

```
data/<series>.tsb   块数据文件
  [0..8)   文件头: magic "TSB1" | version u16 LE (=1) | reserved u16
  随后为块序列，每块：
    块头（48 字节）:
      count    u32 LE   点数
      ts_start i64 LE   首时间戳
      ts_end   i64 LE   末时间戳
      val_min  i64 LE
      val_max  i64 LE
      ts_len   u32 LE   时间戳载荷字节数
      val_len  u32 LE   值载荷字节数
      crc32    u32 LE   CRC-32/IEEE，覆盖 ts_payload ++ val_payload
    ts_payload   delta-of-delta 编码（见下）
    val_payload  整数差分编码（见下）

late/<series>.tsl   乱序缓冲日志（仅 Buffer 模式）
  原始记录，每条 16 字节：ts i64 LE | val i64 LE，按追加顺序
```

块索引**不落盘**：`open` 时扫描全部块头并逐块校验 CRC 重建（见“同步边界”）。

## 编码与溢出处理

- 时间戳：`ts[0]` 原始 8 字节；`ts[1]` 存 delta；`ts[i≥2]` 存 delta-of-delta，
  均以 zigzag varint 编码。
- 值：`v[0]` 原始 8 字节；`v[i≥1]` 存与前值的差分，zigzag varint。
- **溢出（显式处理）**：所有差分在 i128 中计算。差分超出 i64 范围、或恰好等于
  `i64::MIN`（其 zigzag 像 u64::MAX 被保留）时，写入 ESCAPE 标记
  （varint `u64::MAX`）+ 该点的**绝对值** 8 字节。解码端对称处理，从绝对值
  重建游程 delta；任何会使重建值越出 i64 范围的数据一律判为损坏（返回错误），
  不做回绕。

## 同步边界（durability）

- `append` 接受的点仅在内存中，直到 `flush`。
- `flush` 把待写点编码成块、追加到数据文件，然后对数据文件（及非空的 late
  日志）做 `sync`（fsync）。`flush` 返回 Ok 后这些点才持久。
- 崩溃可能留下撕裂尾部（半个块头/半条载荷）。`open` 时逐块扫描：第一个
  不完整、长度不可能或 CRC 校验失败的块即终止扫描，并把文件**截断**到该
  偏移；之前的块全部有效并进入索引。late 日志长度不是 16 的倍数时截断到
  16 字节边界。
- `flush` 返回 I/O 错误后，内存状态可能与磁盘不一致；调用方应丢弃仓库并
  重新 `open`（走恢复路径）再重试。故障注入测试演示了该流程。
- 未 flush 的点对同进程查询可见，但重启即丢失（测试
  `unflushed_points_visible_but_not_durable` 固定了此语义）。

## 乱序语义

同一 series 内时间戳必须严格递增。

- `LateMode::Reject`（默认）：`ts <= 最后接受 ts` 的点被拒绝，
  `ts == last` 记 `duplicate`，`ts < last` 记 `out_of_order`，逐点返回。
- `LateMode::Buffer`：这类点追加到该 series 的 late 日志。查询时与主块
  合并：时间戳冲突时**主流（按序）点优先**；late 日志内部冲突时**先追加者
  优先**（first-write-wins）。late 点永不改写已落盘的块。

## HTTP 接口（本地验证入口）

```
GET  /health
POST /ingest   {"series":"cpu","points":[[ts,val],...]}
POST /flush    {"series":"cpu"}            （或 /flush?series=cpu）
GET  /query?series=cpu&from=0&to=100       （闭区间）
GET  /stats?series=cpu
```

### 请求样例（实际运行记录）

```bash
$ curl -s http://127.0.0.1:18080/health
{"ok":true}

$ curl -s -X POST http://127.0.0.1:18080/ingest -d '{"series":"cpu","points":[[1700000000,42],[1700000001,45],[1700000002,-7],[1700000003,0],[1700000004,9223372036854775807],[1700000004,99],[1700000002,5]]}'
{"series":"cpu","accepted":5,"buffered":0,"rejected":[{"ts":1700000004,"val":99,"reason":"duplicate"},{"ts":1700000002,"val":5,"reason":"out_of_order"}]}

$ curl -s -X POST http://127.0.0.1:18080/flush -d '{"series":"cpu"}'
{"series":"cpu","flushed":true}

$ curl -s "http://127.0.0.1:18080/query?series=cpu&from=1700000000&to=1700000004"
{"series":"cpu","from":1700000000,"to":1700000004,"count":5,"points":[[1700000000,42],[1700000001,45],[1700000002,-7],[1700000003,0],[1700000004,9223372036854775807]]}

$ curl -s "http://127.0.0.1:18080/query?series=cpu&from=1700000002&to=1700000002"
{"series":"cpu","from":1700000002,"to":1700000002,"count":1,"points":[[1700000002,-7]]}

$ curl -s "http://127.0.0.1:18080/stats?series=cpu"
{"series":"cpu","points":5,"pending":0,"blocks":1,"late_points":0,"raw_bytes":80,"file_bytes":89,"late_bytes":0,"compression_ratio":0.8989}

$ curl -s "http://127.0.0.1:18080/query?series=cpu&from=9&to=1"
{"error":"query failed: query range: from > to"}
```

（5 个点的小块压缩比 <1 属正常：48 字节块头 + 8 字节文件头的固定开销
只有在点数上量后才被摊薄，见下方实测。）

## 验收测试设计

`tests/acceptance.rs` 中每个查询结果都与**参考未压缩数组**
（`Vec<(i64,i64)>` 按区间过滤）逐点比对，覆盖：

- 边界查询：整块边界、跨块缝、块首/块末单点、范围外、空结果、`from>to` 报错；
- 负值与过零序列、大幅正负摆动；
- 重复时间戳与乱序点（Reject 与 Buffer 两种模式的语义）；
- i64 极值：`i64::MIN`/`i64::MAX` 的值与时间戳、溢出 ESCAPE 路径；
- 重开持久性：已 flush 数据保留、未 flush 数据丢失；
- 真实压缩比统计（见下）。

`tests/faults.rs` 用 `MockIO` 注入故障：sync 失败、append 失败、读失败、
撕裂尾部截断恢复、CRC 损坏块剔除、late 日志撕裂恢复、恢复后继续写入。

`tests/http_api.rs` 起真实服务器（临时端口）走完整 HTTP 流程。

## 实测运行记录

环境：Linux x86_64，rustc 1.96.0，零外部依赖。

```
$ cargo test
test result: ok. 10 passed; 0 failed   (codec/json 单元测试)
test result: ok. 9 passed; 0 failed    (tests/acceptance.rs)
test result: ok. 7 passed; 0 failed    (tests/faults.rs)
test result: ok. 1 passed; 0 failed    (tests/http_api.rs)
合计 27 个测试全部通过；cargo build --release 0 警告。

$ cargo test --test acceptance compression_ratio_report -- --nocapture
regular 1s-cadence data: 10000 points, raw 160000 bytes -> file 30105 bytes, ratio 5.31x (157 blocks)
jittered random data:    10000 points, raw 160000 bytes -> file 88653 bytes, ratio 1.80x
```

压缩比说明：规则 1 秒间隔数据（delta-of-delta 恒为 0 → 每点 1 字节级）
约 **5.3x**；抖动随机数据约 **1.8x**。未通过项：无。

## 代码结构

```
src/codec.rs    块编码：DoD 时间戳、差分值、zigzag varint、ESCAPE 溢出、CRC32
src/io.rs       FileIO trait；FsIO（真实文件系统）；MockIO（故障注入）
src/storage.rs  仓库：块索引、区间读、乱序两种模式、崩溃恢复截断
src/json.rs     最小 JSON 解析（仅供 ingest API）
src/http.rs     最小 HTTP 服务（std::net，无框架）
src/main.rs     服务器入口（命令行参数）
tests/          验收 / 故障注入 / HTTP 集成测试
```
