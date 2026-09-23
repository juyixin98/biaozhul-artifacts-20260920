# 有界内存外排序（Bounded-Memory External Sort）

纯 Rust 后端项目（**零第三方依赖**，仅标准库）：对超出内存预算的换行分隔文本做
**稳定的外排序**，存储完全由文件支持，提供本地 HTTP 验证入口；磁盘格式、同步边界
与恢复语义都有明确定义，I/O 层可注入故障用于测试崩溃恢复。

不包含任何前端代码。

---

## 1. 功能总览

- **有界内存**：内存预算 `mem` 同时覆盖记录区和 I/O 缓冲区（含归并阶段的多路读缓冲
  + 一路写缓冲）；响应头 `X-Sort-Peak-Bytes` 如实报告估算峰值。
- **稳定排序**：按复合键（多个制表符字段，各自可升/降序）比较；复合键相等时严格按
  原始输入序号决胜。
- **分段落盘**：记录在内存中积累到预算边界即做一次稳定内存排序，写成带 CRC 的
  “run” 分段。
- **多路、多轮归并**：每轮 `k` 路归并（`k` 受内存预算钳制），轮次数量由
  `ceil` 分层决定；验收用极小内存强制 3 轮。
- **崩溃恢复**：每个分段、每个归并输出、每次状态变更都立即 fsync 并写入清单；
  重跑时复用所有完好的产物，从第一个损坏/缺失处续做。
- **故障注入**：`FailingFs` 包装真实文件系统，可在第 N 次分段 fsync、或输入读取达到
  指定字节数时确定性地注入 I/O 错误。
- **超大单行**：行读取器缓冲区可按倍数增长，单行远大于内存预算时产生“单记录分段”，
  不截断、不报错。
- **本地 HTTP 入口**：无依赖手写 HTTP/1.1，支持 POST 提交、GET 恢复、统计响应头。

## 2. 目录结构

```text
Cargo.toml
src/
  main.rs        CLI：serve / verify / verify-run
  lib.rs         库入口
  error.rs       统一错误类型（含 HTTP 状态码映射）
  crc32.rs       CRC-32（ISO-HDLC），逐帧与聚合校验
  fs.rs          可注入 I/O 层：Fs/RFile/WFile + RealFs + FailingFs + 缓冲读写
  format.rs      run 分段磁盘格式（魔数/帧/CRC/trailer）
  lreader.rs     流式行读取器（\n 与 \r\n，任意长单行）
  manifest.rs    作业清单（崩溃恢复事实来源）
  key.rs         复合键解析与稳定比较器
  engine.rs      分段生成、多轮多路归并、恢复、内存预算
  server.rs      本地 HTTP 验证服务
tests/
  end_to_end.rs  14 个端到端/故障注入/HTTP 集成测试
examples/
  requests.http.md   原始 HTTP 请求样例与 curl 用法
scripts/
  acceptance.sh      一键验收脚本（构建、测试、起服务、对照参考排序、损坏恢复）
```

## 3. 构建与运行

```bash
cargo build --release
# 调试构建直接 cargo build 即可；CLI 位于 target/debug/extsort
```

启动 HTTP 服务（默认监听 `127.0.0.1:8080`，仓库目录 `./repo`）：

```bash
./target/release/extsort serve 127.0.0.1:8080 ./repo
```

内存参考排序校验（输入 + 外排序输出，按复合键在内存中稳定排序后逐字节比较）：

```bash
./target/release/extsort verify input.txt output.txt "1:asc;2:asc"
```

校验单个分段文件（魔数 / 逐帧 CRC / 聚合 CRC / trailer）：

```bash
./target/release/extsort verify-run repo/jobs/demo/runs/run-000001.exs
```

## 4. HTTP 接口

| 方法/路径 | 说明 |
|---|---|
| `GET /healthz` | 存活检查，返回 `ok` |
| `POST /sort?job=&mem=&buf=&fanin=&key=` | 提交输入（请求体即原始文本），同步返回排序结果 |
| `GET /sort?job=&mem=&buf=&fanin=&key=` | 不带正文；未完成则续做，已完成则直接返回结果 |
| `GET /sort?job=&result=1` | 仅流式取回已完成作业的 `output.txt` |

查询参数：

| 参数 | 默认 | 含义 |
|---|---|---|
| `job` | `default` | 作业 ID：`[A-Za-z0-9_-]{1,128}` |
| `mem` | 65536 | 总内存预算（字节，含缓冲） |
| `buf` | 4096 | I/O 缓冲区（字节） |
| `fanin` | 8 | 每路归并输入上限，实际取 `min(请求值, (mem-余量)/buf - 1)`，下限 2 |
| `key` | `0:asc` | 复合键：`字段:方向`，多段以 `;` 分隔；字段 0 为整行，n≥1 为第 n 个制表符字段，方向 `asc`/`desc` |

成功响应头（示例来自验收运行，`mem=2048&buf=256`、1200 行）：

```text
X-Sort-Records: 1200
X-Sort-Runs: 145
X-Sort-Merge-Rounds: 3
X-Sort-Merge-Steps: 31
X-Sort-Runs-Reused: 0
X-Sort-Merges-Reused: 0
X-Sort-Fanin: 6
X-Sort-Peak-Bytes: 1792
X-Sort-Resumed: 0
```

错误码：`400` 参数非法；`404` 作业/结果不存在；`409` 用不同配置恢复已有作业或
POST 带正文重放；`500` 分段 CRC 失败等存储错误。

完整原始请求见 [`examples/requests.http.md`](examples/requests.http.md)。

## 5. 磁盘格式

仓库布局：

```text
<repo>/
  jobs/<job>/
    input.txt                 原始输入（POST 时原子落盘）
    manifest.txt              恢复清单（原子重写）
    output.txt                最终排序结果（每行一条，末尾 \n）
    runs/run-000001.exs ...   分段
    merges/m-r00-s000.exs ... 各轮归并输出（与 run 同格式）
```

run 文件格式（所有整数小端序）：

```text
偏移 0:   8 字节  魔数 b"EXTSORT1"
每帧:    16 字节帧头  seq:u64 | ulen:u32 | crc:u32
          ulen 字节   有效载荷（原始行，不含换行）
结尾:     8 字节  尾魔数 b"EXEND001"
         12 字节尾载荷 count:u64 | aggr_crc:u32
          4 字节  尾载荷自身的 CRC-32
```

- **逐帧 CRC**：CRC-32 覆盖 12 字节帧头 + 有效载荷。
- **聚合 CRC**：对“每帧帧头+有效载荷”的拼接序列做流式 CRC-32，检测丢帧/重排/损坏
  （即使个别帧 CRC 偶然仍匹配）；与帧计数一起写入 trailer。
- 帧与 trailer 不能只看首字节区分（帧头首字节任意），读取器一次探测 8 字节再判定。

## 6. 同步边界（崩溃安全）

每次“提交”严格按以下顺序执行：

1. 数据写到同目录 `*.tmp`；
2. `fsync(tmp)`（`WFile::sync_all`，故障注入点也在此）；
3. `rename(tmp → 正式名)`；
4. `fsync(父目录)`，保证目录项持久化；
5. 把新产物追加/更新进 `manifest.txt`，同样走 **写临时文件 → fsync → rename →
   fsync 目录** 的原子替换。

语义推论：

- 作业崩溃时，清单中引用的每个 run 都已完成数据 fsync + 目录 fsync；
- 正在写的分段只存在于 `*.tmp`，启动时统一清理，绝不参与归并；
- 恢复时先逐个 `verify_run`（魔数/帧 CRC/聚合 CRC/计数），仅保留**连续前缀**中的
  完好分段，其后分段及全部归并输出作废，输入重放跳过已确认记录数；
- 归并阶段同样以“步骤”为提交单位；恢复时按确定性归并计划逐步骤核对（轮次、序号、
  文件名、输入列表、文件校验），仅复用严格匹配的前缀；
- 空输入直接生成空 `output.txt` 并标记 `state DONE`。

## 7. 内存预算如何计算

- 记录占用按 `48 + 行字节 + 行字节/8` 保守估算（`Record`/`Vec` 开销 + 分配余量）。
- 分段刷盘阈值：`mem - buf - 256`（256 为固定余量）。
- 归并阶段预留 `k` 个读缓冲 + 1 个写缓冲：
  `有效 k = min(请求 fanin, (mem - 余量)/buf - 1)`，下限 2。
- 超大单行不受刷盘阈值限制：读到 EOF 后仍会作为独立分段正确落盘。
- `X-Sort-Peak-Bytes` 为分段阶段记录峰值与归并缓冲估算的较大者；它是**估算值**
  （真实进程 RSS 还包含分配器元数据），用途是验证预算逻辑而不是精确测量。

## 8. 故障注入（可注入 I/O 层）

`src/fs.rs` 中：

- `Fs` / `RFile` / `WFile` 是引擎唯一接触磁盘的接口；
- `RealFs` 锚定仓库根，拒绝绝对路径与 `..` 穿越；
- `FailingFs` 持有共享 `FailState`：
  - `fail_on_sync = Some(N)`：第 N 次对 **run/merges 分段**的 `sync_all` 返回错误
    （输入文件、清单的同步不计数，保证阈值语义稳定）；
  - `fail_read_path` + `fail_on_read_bytes`：对路径匹配的读取在交付指定字节后失败
    （按字节计数，与缓冲大小无关）。

集成测试 `tests/end_to_end.rs` 覆盖：分段 fsync 失败后恢复、归并中途 fsync 失败后
前缀复用、输入读取中断后恢复、分段载荷/trailer 损坏检出与重建、遗留 `*.tmp` 清理。

## 9. 自动化测试与验收

```bash
cargo test                    # 16 个单元测试 + 14 个端到端测试
bash scripts/acceptance.sh    # 完整验收（构建/测试/起服务/对照/损坏恢复）
```

最近一次真实运行结果记录在 [`RUNLOG.md`](RUNLOG.md)，包含命令、关键响应头、
通过/未通过项。

## 10. 设计取舍与边界

- 手写 HTTP 仅用于本地验证：每连接单请求、`Connection: close`、单请求体 2 GiB 上限，
  不适合生产暴露。
- 行定义为字节序列，按字节序比较（非 UTF-8 文本也能工作）；字段以制表符分隔，
  超界字段按空值处理。
- 归并选择采用线性扫描 `k` 个游标（k 很小，通常 ≤8），换取实现简单、可审计；
  数据量更大时可替换为 loser tree，接口不变。
- 无压缩（帧头保留 `ulen`，当前即未压缩长度），格式版本 `EXTSORT1` / 清单 `v1`。
