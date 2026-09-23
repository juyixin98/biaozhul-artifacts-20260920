# extsort — 有界内存、文件支持的稳定外排序（纯后端）

一个用 Rust 标准库实现（**零第三方依赖**）的外部排序服务：

- **有界内存**：内存预算显式包含读缓冲、写缓冲、归并车道缓冲与溢出工作区；
  超过额度的记录在分段阶段就落盘，绝不会把整行读进内存。
- **稳定**：按复合键比较，重复键严格按**原始输入序号**（sequence number）排序。
- **复合键升降序**：如 `1:asc,2:desc`，字段 0 表示整行（兼容 `sort(1)`）。
- **分段落盘 + 多路归并**：map 阶段产生有序 level-0 段，merge 阶段按预算车道数
  做 k 路归并，可强制产生**多轮**归并。
- **崩溃恢复**：每完成一个段 / 一个归并批，就把清单（manifest）原子写并
  fsync；失败后从已提交的分段继续，未提交的 `*.tmp` 被丢弃。
- **可注入 I/O 层**：所有磁盘操作走 `Vfs` trait，测试 / HTTP 头可注入
  创建、写、sync、改名、读故障。
- **段文件完整性**：魔数 + 每块 CRC-32 + 尾部记录计数，能检测位翻转与截断。
- **本地 HTTP 验证入口**：纯 `std::net` 实现，无前端。

---

## 1. 构建

```bash
cargo build --release
# 二进制：target/release/extsort
```

无需联网、无外部 crate。要求 Rust ≥ 1.75（在 1.98 上开发验证）。

## 2. 快速开始（CLI）

```bash
# 基本：整行升序
./target/release/extsort sort --input data.txt --output out.txt

# 复合键：第 1 列升序、第 2 列降序；用极小内存强制多轮归并
./target/release/extsort sort \
  --input data.csv --output out.csv \
  --spec "1:asc,2:desc" --delim , \
  --budget 163840 --lanes 2 --keep-temp

# 失败后续跑（同一 --root/--job）
./target/release/extsort resume --root ./repo --job local --output out.csv
```

输出统计形如：

```
done: records=20000 level0_runs=17 merge_passes=5 merge_batches=20 output_bytes=358868
```

- `level0_runs`：map 阶段产生的分段数；
- `merge_passes`：归并层数（多轮）；
- `merge_batches`：归并批次数。

### 键规格语法

```
spec       := [ field-spec ("," field-spec)* ]
field-spec := 字段序号 [ ":" (asc|desc) ]     # 默认 asc
```

- 字段 `0` = 整行；`1` = 第一列（按单字节分隔符切分，默认 `,`）。
- 不存在的列按空字节处理。
- 字节序字典序比较（对合法 UTF-8 即码点序）；`desc` 只反转该字段方向。
- 各字段全等时按输入序号升序——这是稳定性的来源。

### 输出约定

每条记录输出后补一个 `\n`（含最后一条）；输入中的 CRLF 会规范化为 LF，
因此“无尾换行的最后一行”和“空输入”都有确定、可对照的结果。

---

## 3. HTTP 验证入口

```bash
./target/release/extsort serve --root ./httproot --addr 127.0.0.1:8080
```

| 方法与路径 | 作用 |
|---|---|
| `GET  /healthz` | 存活检查 |
| `POST /jobs?spec=…&budget=…&lanes=…` | 请求体即原始输入；创建作业并运行 |
| `POST /jobs/{id}/resume` | 对故障中止的作业从已提交分段续跑 |
| `GET  /jobs` | 列出作业 id |
| `GET  /jobs/{id}` | 状态 JSON（`status.json`） |
| `GET  /jobs/{id}/output` | 排序结果字节 |
| `DELETE /jobs/{id}` | 删除作业目录 |

查询参数：`spec`、`delim`（单字节，默认 `,`）、`budget`（字节）、
`lanes`（归并扇入，≥2，默认 4）、`keep_temp=1`。

故障注入：在 `POST /jobs` 上加请求头 `X-Fault-Inject`（语法见
[`examples/http-requests.sh`](examples/http-requests.sh) 与第 7 节）。

---

## 4. 磁盘格式与目录布局

作业目录 `<root>/jobs/<job-id>/`：

```
input.dat                 原始输入（上传后原子落盘）
output.txt                排序结果（state=done 后存在）
manifest.txt              持久化作业状态（提交点）
status.json               面向 HTTP 的状态快照
level0/run-00000.run      map 阶段有序分段
level0/run-00001.run
level1/m-1-00000.run      各轮归并输出
level2/...
*.run.tmp                 未提交的临时文件，恢复时一律删除
```

### 段（run）文件格式

```
magic   : 8 字节 = b"EXTSORT\x02"
frames  : 0..N 个记录帧
trailer : 帧类型字节 FRAME_TRAILER(0x20) + u64 LE 记录数
```

每个帧以 1 字节类型开头，使顺序读取能区分记录与尾部：

```
FRAME_CHUNK = 0x10      FRAME_TRAILER = 0x20
```

chunk 帧：

```
ftype   u8      = FRAME_CHUNK
flags   u8      bit0=FIRST（记录首块） bit1=CONT（后面还有块）
length  u32 LE  负载长度（≤ CHUNK_MAX = 16 KiB）
crc32   u32 LE  负载的 CRC-32/IEEE
payload length 字节
```

FIRST 块负载：

```
u64 LE  seq        原始输入序号（稳定 tie-break）
u32 LE  key_count  键分量个数
每个键: u32 LE len + 键字节
字节流  value 前缀（记录本身，不含行结束符）
```

后续块只装 value 续接字节。这样**任意长度**的一行都能以有界内存落盘：
超长行的键放在 FIRST 块，value 逐块流式写出 / 读入。

完整性：每块带 CRC，消费即校验；尾部重复记录数以检测 torn tail；
魔数检测错误 / 截断文件。

---

## 5. 同步（durability）边界

- 段写入流程：写 `*.run.tmp` → `flush` + `fsync` → 关闭 → **原子 rename**
  为 `*.run` → 对父目录再 `fsync`（见 `RealVfs::rename`）。
  因此 rename 是一次崩溃一致的提交点。
- 但“文件已 rename”不等于“作业已采用它”：**manifest 才是逻辑提交点**。
  每次写完一个 map 段或一个归并批，都把 `manifest.txt.tmp` 写好 fsync 后
  原子 rename 成 `manifest.txt`。崩溃后：
  - manifest 记录的段 → 校验后复用；
  - 磁盘上有但 manifest 未登记的段 / `*.tmp` → 删除重做；
  - **可恢复的 I/O 故障不会把作业推进终态**：故障只写入 `status.json` 供观察，
    manifest 停留在最后一次提交点（HTTP 用 `POST /jobs/{id}/resume`，CLI 用
    `resume`）。真正的磁盘损坏由 CRC / 计数校验独立拦截并报错；
  - map 阶段：重开输入，跳过 `map_consumed` 条已持久化记录；
  - merge 阶段：`runs` 是当前层尚未归并的段，`merge_done` 是本轮已提交的
    上层输出。每个批只有在其输出 run 已 fsync 后才从 manifest 移除输入，
    且输入文件要等**整轮**切换层级时才回收——因此崩溃最多重做**当前未提交的
    那一个批**，已提交批不会丢数据。

### manifest 文本格式

```
EXTSORT-MANIFEST/1
job <id>
state <map|merge|done|failed>
level <n>
total_records <n>
map_consumed <n>
spec <键规格>
delim <byte>
budget <bytes>
max_lanes <n>
keep_temp <0|1>
run <name> <count>          # 当前层就绪 / 待归并段
merge_done <name> <count>   # 本轮已产出的上层段
error <可选失败原因>
```

---

## 6. 内存预算如何计算（含缓冲区）

`Budget::plan(total, max_lanes)` 先扣固定成本，再把剩余额度给驻留记录：

```
固定 = 读缓冲 16 KiB + 输出缓冲 16 KiB + 溢出工作区 48 KiB
每车道 = 续读缓冲 8 KiB + 车道头块 16 KiB
驻留额度 = total - 固定 - 车道数 * 每车道
```

车道数在预算紧张时下调（但**绝不低于 2**：1 路归并不会减少段数，会死循环），
下限 `MIN_BUDGET = 固定 + 2*每车道 + 4 KiB 驻留额度`。驻留记录字节达到额度
即落盘一个段。要“用小内存强制多轮归并”，只需把 `--budget` 调到接近下限、
`--lanes 2`，并让数据量远大于驻留额度（见验收测试与示例）。

> 说明：单个记录的**键**必须能放进 FIRST 块头窗口（默认约 16 KiB）。
> value 可任意长（流式）。若键本身比头窗口还大，作业以 `key_too_large`
> 明确失败，而不是悄悄超内存。

---

## 7. 可注入 I/O 与故障语法

所有磁盘操作经过 `Vfs` trait；生产用 `RealVfs`，测试 / HTTP 用
`FaultVfs` 装饰。故障规则语法（逗号分隔多条）：

```
rule := kind[ ":" needle ][ ":" count ]
kind := write_fail | sync_fail | rename_fail | create_fail | read_fail
```

- `needle`：对受影响路径做子串匹配（缺省匹配任意路径）；
- `count`：连续命中的失败次数（缺省 1，0 表示禁用）。

示例：

- `--fault "write_fail:run-00001"`：写第二个 map 段时失败（第 0 段保留）；
- `--fault "create_fail:m-1-"`：创建首个 level-1 归并输出时失败；
- HTTP：`-H 'X-Fault-Inject: sync_fail:run-00003.run.tmp'`。

---

## 8. 测试

```bash
cargo test               # 27 单元测试 + 14 端到端验收测试
cargo clippy             # 无警告
```

`tests/acceptance.rs` 覆盖验收点：

- **多轮归并 + 对照内存参考排序**（20k 行、极小预算、asc 与 asc+desc）；
- 复合键升降序与**重复键按序号稳定**；
- **空输入**、无尾换行的单行（规范化为带 `\n`）；
- **超大单行**（300 KiB，远超预算，逐块流式，字节无损）与“普通记录 + 巨型行混排”；
- 整行键遇到超大行时 `key_too_large` 显式失败；
- map / merge 阶段注入故障后 **resume**，结果与内存参考一致；
- 段负载位翻转（**CRC 检出**）、段尾截断（torn tail 检出）、坏魔数；
- 崩溃遗留 `*.tmp` 在恢复时被丢弃；
- 预算低于下限被拒绝。

内存参考实现见 `src/reference.rs`：同样的复合键比较器 + 序号 tie-break，
一次性驻留内存排序，作为对照基准。

---

## 9. 模块地图

| 文件 | 职责 |
|---|---|
| `src/error.rs` | 统一错误类型 / HTTP 状态映射 |
| `src/crc.rs` | CRC-32（IEEE，0xEDB88320） |
| `src/io.rs` | `Vfs`/`VFile`、`RealVfs`、`FaultVfs`、原子写 |
| `src/key.rs` | 复合键提取与升/降序比较、规格解析 |
| `src/budget.rs` | 内存预算（含缓冲） |
| `src/sortfile.rs` | 段文件帧格式、流式读写、CRC 校验 |
| `src/scanner.rs` | 有界行扫描器 + 超长行流式 drain |
| `src/repository.rs` | 作业目录、manifest/status、恢复 |
| `src/sort.rs` | map + 多轮 k 路归并引擎 |
| `src/reference.rs` | 内存参考排序 |
| `src/server.rs` | 本地 HTTP 验证入口 |
| `src/testutil.rs` | 集成测试用的公共驱动 |
| `tests/acceptance.rs` | 端到端验收 |

## 10. 范围与非目标

- 纯后端：不含任何前端 / HTML 页面。
- 分隔符为单字节、无引号转义的“类 CSV”；不做字符集转换（按字节比较）。
- 单机本地文件系统；不实现分布式 shuffle。
