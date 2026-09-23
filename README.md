# 外存排序-归并连接服务（External Sort + Merge Join）

纯 JDK 实现（`com.sun.net.httpserver.HttpServer`，**零第三方依赖**）的 JSONL 等值连接 HTTP 服务。
连接键为 **整数或 NULL**；内存预算可配置；数据量超过预算时使用外存排序与归并连接；
重复键产生**笛卡尔积**；**NULL（含键缺失）永不匹配**。

## 1. 环境要求

- **JDK 17+**（仅需 `java` / `javac`；已在 Temurin OpenJDK 17.0.20 上验证）。
- 无 Maven/Gradle、无第三方 jar。构建与测试脚本只调用 `javac`/`java`。

检查：

```bash
java -version
```

如 `java` 不在 PATH，用 `JAVA_HOME` 指定（本仓库开发机即用此方式）：

```bash
export JAVA_HOME=/path/to/jdk-17
```

## 2. 构建

```bash
./build.sh          # javac 编译到 target/classes
```

## 3. 启动服务

```bash
./run.sh
# 可选环境变量：
#   JOIN_PORT=8080          监听端口（默认 8080）
#   JOIN_CONCURRENCY=2      并行作业数（默认 2）
#   JOIN_DATA_DIR=./data    数据根目录（默认 ./data）
```

启动后：

- 输入文件必须位于 `JOIN_DATA_DIR` 之内（请求里给相对该目录的路径，或目录内的绝对路径；目录穿越会被拒绝）。
- 临时文件：`$JOIN_DATA_DIR/_tmp/<jobId>/` —— 作业**结束（成功/失败/取消）后一律删除**。
- 结果文件：`$JOIN_DATA_DIR/results/<jobId>.jsonl` —— 成功后保留，失败/取消时删除。

## 4. HTTP 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET  | `/health` | 健康检查 |
| POST | `/join` | 提交连接作业，返回 `202` + 作业 ID |
| GET  | `/join` | 列出所有作业 |
| GET  | `/join/{id}` | 查询作业状态（失败/取消后**错误信息保留**） |
| POST | `/join/{id}/cancel` | 请求取消（返回 `202`） |
| GET  | `/join/{id}/result` | 下载结果 JSONL（仅 `COMPLETED` 返回 `200`） |

### POST /join 请求体

```json
{
  "leftInput": "examples/users.jsonl",
  "rightInput": "examples/orders.jsonl",
  "leftKeyColumn": "k",
  "rightKeyColumn": "k",
  "memoryBudgetBytes": 65536
}
```

- `memoryBudgetBytes` 可选，默认 `67108864`（64 MiB），必须 ≥ 1。
- 输入为 JSONL（每行一个 JSON 对象，UTF-8）。键值必须是整数；`null` 或缺列视为 SQL NULL；
  其它类型（字符串、浮点、数组、对象）直接报错并指出行号。
- 结果每行形如 `{"left":<左表原始行>,"right":<右表原始行>}`，原始行字节原样嵌入。

### 状态响应示例（COMPLETED）

```json
{
  "id": "job-1790149978193-1",
  "status": "COMPLETED",
  "outputRows": 6,
  "outputBytes": 412,
  "resultUrl": "/join/job-1790149978193-1/result",
  "sort": {
    "left":  {"totalRows": 6, "nullRowsDropped": 2, "spilled": false, "initialRunCount": 0, "mergePasses": 0, "initialSpillBytes": 0, "totalTempBytesWritten": 0},
    "right": {"totalRows": 6, "nullRowsDropped": 1, "spilled": true,  "initialRunCount": 3, "mergePasses": 0, "initialSpillBytes": 12345, "totalTempBytesWritten": 12345}
  },
  "join": {"groupSpillCount": 2, "groupSpillBytes": 45678, "blockNestedLoopBlockCount": 30}
}
```

`CANCELLED` / `FAILED` 时 `error.type` 与 `error.message` 仍可通过 GET 读取。

## 5. 请求样例（curl）

```bash
# 1) 提交
curl -s -X POST localhost:8080/join \
  -H 'Content-Type: application/json' \
  --data @examples/join-request.json

# 2) 轮询（用上一步返回的 id）
curl -s localhost:8080/join/job-xxx

# 3) 取结果
curl -s localhost:8080/join/job-xxx/result

# 4) 取消
curl -s -X POST localhost:8080/join/job-xxx/cancel
```

### 大热键 + 极小预算的验收样例

```bash
examples/gen-large.sh 200000          # 在 data/gen 下各生成 20 万行，约 90% 集中在热键 k=1
curl -s -X POST localhost:8080/join -H 'Content-Type: application/json' -d '{
  "leftInput":"gen/left.jsonl",
  "rightInput":"gen/right.jsonl",
  "leftKeyColumn":"k","rightKeyColumn":"k",
  "memoryBudgetBytes":1048576
}'
```

此时单个键 k=1 的分组（每侧约 180,000 行、~18 MB）**远大于 1 MiB 预算**，
状态里会看到 `"groupSpillCount": 2` 与正数 `"blockNestedLoopBlockCount"`。

## 6. 算法与内存边界

1. **生成有序游程（run generation）**：单遍流式读入，缓冲达到 `memoryBudgetBytes`
   就按键排序并溢写一个二进制游程文件（`key:8, len:4, raw[]`，NULL 行计数后丢弃）。
   整表装得下时完全不产生临时文件。
2. **k 路归并**：小顶堆合并游程；单次最多打开 `MAX_OPEN_RUNS=64` 个文件，
   游程过多时自动多趟（multi-pass）归并。
3. **排序-归并连接**：顺序取两侧相等键的分组做笛卡尔积。分组本身可能超过预算：
   - 两侧都装得下：纯内存嵌套循环；
   - 一侧装得下：物化小侧，溢写侧单遍流式扫描；
   - **两侧都装不下（热键大于内存）**：两侧分别溢写为分组文件，做
     **分块嵌套循环（block nested-loops）**——按预算加载 A 的一块，整文件扫描 B；
     内存占用有上界，输出多重集精确。单条记录本身大于预算时无法切分，
     该记录单独驻留（这是内存下界，文档明示的唯一例外）。
4. 预算的计量为缓冲记录的 JSON 字节数 + 每条 ~64B 对象开销的保守估计，
   属于**软预算**（缓冲量的上界，不含 JVM 自身开销）。

## 7. 取消语义

- `POST /join/{id}/cancel` 置取消标志并中断工作线程；排序/归并/连接的行循环每
  4096/8192 行检查一次，阻塞 I/O 被中断时同样转为 `CANCELLED`。
- 取消后：删除部分结果文件、递归删除 `_tmp/<jobId>/`；
  `GET /join/{id}` 保留错误信息（`error.type=cancelled`）；
  `/result` 返回 `410`；重复取消返回 `409`。

## 8. 自动化测试

```bash
./test.sh
```

无测试框架，全部是带断言的 `main` 程序（非零退出即失败），分两层：

- `join.EngineTests`（引擎级）：
  - 基本语义（笛卡尔积、NULL 不匹配、缺列即 NULL、计数）；
  - 多组随机数据与暴力参考实现逐字符串比对**输出多重集**（大内存 / 512B 预算 / 128B 预算）；
  - **热键验收**：单键分组两侧各 5–9 KB、预算 400B，验证 60×50=3000 行多重集精确、
    两侧分组溢写、分块嵌套循环被触发、分组临时文件用完即删；
  - 非法 JSON / 非整数键报错带行号；
  - 服务层取消：作业转为 `CANCELLED`、临时目录清空、部分结果删除、错误信息保留。
- `join.ApiIT`（真实 HTTP 端到端）：起在随机空闲端口，覆盖健康检查、参数校验、
  路径穿越拒绝、提交/轮询/下载、双侧溢写 + 热键作业的结果多重集与参考实现一致、
  HTTP 取消与清理。

## 9. 锁定依赖

- 运行时依赖：**无**。仅使用 JDK 自带模块（`java.base`、`jdk.httpserver`）。
- 构建期依赖：JDK 自带 `javac`（`--release 17`）；脚本为 POSIX shell + `find`。
- 已验证 JDK：Temurin OpenJDK `17.0.20.1+1`（Linux x86_64）。
- 依赖锁定清单见 [`deps.lock`](./deps.lock)：编译/测试/运行均不获取任何第三方制品。

## 10. 目录结构

```
src/main/java/join/   Json/Record/RunFiles/ExternalSorter/Group/JoinEngine/
                      Job/JobService/ApiServer/TaskContext/...
src/test/java/join/   EngineTests.java, ApiIT.java, EngineTestsSupport.java
examples/             小样本 JSONL、请求体、大数据生成脚本
build.sh test.sh run.sh
```

## 11. 实测记录（2026-09-23，Temurin 17.0.20，Linux x86_64）

- `./test.sh`：**引擎测试 30/30 通过，HTTP 端到端测试 32/32 通过**；`-Xlint:all` 零警告。
- 小样本（`examples/`，预算 64 KiB）：6 行左表 × 6 行右表，输出 7 行
  （k=10 笛卡尔 2×3、k=20=1；3 条 NULL/缺列行全部丢弃），无溢写。
- **热键验收（单键组 ≫ 内存预算）**：每侧 10,000 行（热键 k=1 约 980 行/侧，行均 ~78B，
  分组 ~80 KB），预算 **16 KiB**：
  - 排序阶段两侧各产生 84 个初始游程、**1 趟多趟归并**、各溢写 875 KB；
  - 连接阶段 `"groupSpillCount": 2`（热键两侧分组都溢写）、
    `"blockNestedLoopBlockCount": 8`（分块嵌套循环）；
  - 输出 909,797 行 / ~154 MB；用独立 Python 暴力实现按排序后 JSON 元组比对，
    **结果多重集完全相等**，NULL 参与行数 0；
  - 作业结束后 `data/_tmp/<jobId>/` 下文件数为 0，结果文件保留。
- **取消验收**：每侧 400,000 行、预算 4 KiB（取消前已产生 2,838 个临时文件、12 MB 溢写）；
  `POST /cancel` 后 330 ms 内作业变为 `CANCELLED`，错误信息保留，
  `_tmp/<jobId>/` 整个目录删除，部分结果文件不存在，`/result` 返回 410，重复取消返回 409。

## 12. 已知限制 / 未完成项

- 预算是缓冲数据的软上界（按行字节估算），不是 JVM 堆硬限制；建议堆大小 `-Xmx` 略大于预算。
- 单条记录大于整个预算时必须整条驻留内存（无法切分单行 JSON）。
- 键类型只接受 64 位整数；浮点、字符串键不在需求内，会被明确拒绝。
- 结果文件成功后保留在磁盘（不做 TTL 清理）；进程重启后内存中的作业状态丢失
  （结果文件仍在 `results/`，但不再有状态记录）。
- 未内置鉴权/TLS；按纯后端内网工具定位，需要时放在反向代理之后。
