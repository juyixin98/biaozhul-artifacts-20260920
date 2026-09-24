# 多维位图索引服务（Multi-Dimensional Bitmap Index）

纯后端离线数据集位图索引服务：对枚举列构建位图索引，支持 **eq / in / AND / OR /
NOT** 布尔查询、**软删除与恢复掩码**，并输出索引空间统计（未压缩与 RLE 压缩）。

- **纯 JDK 实现，零第三方依赖**：HTTP 用 JDK 内置
  `com.sun.net.httpserver.HttpServer`；JSON、CSV、RLE 编解码均为手写。
- **行 ID 语义稳定**：行 ID = 数据加载顺序的 0 基下标。删除只清存活掩码位，
  位图长度不收缩、行号不重排、不被压缩“悄悄改变”；任何 RLE 编码都可按原始
  下标无损还原（建索引时自动做往返校验）。
- **NOT 仅在当前存活行全集内取补**：`NOT x = alive AND (NOT x)`，已删除行
  永远不会被 NOT 捞回来。

## 目录结构

```
src/bitserver/
  Bitmap.java        固定长度位向量（long[]，set/and/or/andNot/not/cardinality…）
  RleCodec.java      位图 RLE 无损压缩（firstBit + varint 游程；保下标）
  BitmapIndex.java   多维枚举列索引（列 -> 值 -> 位图）+ 空间统计
  QueryEngine.java   布尔表达式求值（eq/in/and/or/not/alive，存活语义内建）
  IndexService.java  线程安全服务层：加载/查询/删除/恢复/统计（synchronized）
  Json.java          手写 JSON 解析/序列化
  Csv.java           RFC4180 风格 CSV 解析（引号、转义、CRLF）
  Main.java          JDK HttpServer 入口与路由
  TestRunner.java    零依赖自动化测试（含随机表达式 vs 行扫描、HTTP 回环）
data/sample.csv      示例数据集（20 行 × 5 列）
build.sh test.sh run.sh
REQUESTS.md          全部 HTTP 请求样例（curl）
DEPENDENCIES.md      依赖与工具链锁定说明
RUN_LOG.md           实际运行/测试记录（真实输出）
```

## 环境要求与启动命令

需要 **JDK 17+**（验证版本：OpenJDK 17.0.20.1）。无需 Maven/Gradle，无第三方 jar。

```bash
./build.sh                                   # 编译到 out/
./test.sh                                    # 运行全部自动化测试
./run.sh 8080                                # 启动（启动后用 POST /load 加载）
./run.sh 8080 data/sample.csv                # 启动并自动加载 CSV
# 等价手动命令：
#   javac -encoding UTF-8 -d out $(find src -name '*.java')
#   java -cp out bitserver.Main 8080 --autoload data/sample.csv
```

启动后：

```bash
curl -s http://localhost:8080/        # 接口说明
curl -s http://localhost:8080/health  # {"status":"ready",...}
```

## 数据模型

- 所有列均按**枚举（类别）列**处理；空字段是空字符串这个合法类别值。
- 加载方式（`POST /load`，整体替换；新数据集行 ID 从 0 重新编号）：
  - `{"csv":"city,grade\nBJ,A\n..."}` —— 内嵌 CSV（首行表头）；
  - `{"csvPath":"/abs/path/data.csv"}` —— 服务端文件路径（也可命令行 `--autoload`）；
  - `{"columns":["city","grade"], "rows":[["BJ","A"],...]}` —— 结构化 JSON。
- 数字/布尔标量在 `eq`/`in`/`rows` 中按标准文本形式匹配
  （`true`/`false`、整数十进制）。

## 表达式与 HTTP 接口

表达式（JSON 树）：

```jsonc
{"op":"eq","col":"city","value":"BJ"}
{"op":"in","col":"city","values":["BJ","SH"]}
{"op":"and","args":[ <expr>, ... ]}     // 空 args = 全集
{"op":"or", "args":[ <expr>, ... ]}     // 空 args = 空集
{"op":"not","arg":<expr>}               // = alive AND NOT arg
{"op":"alive"}                          // 当前存活全集
```

| 方法/路径 | 说明 |
|---|---|
| `GET /` | 接口说明 |
| `GET /health` | 健康/加载状态（未加载 `no-data`） |
| `POST /load` | 加载/替换数据集 |
| `POST /query` | `{"where":<expr>, "limit"?:n}`，返回命中存活行 `ids`（升序、原始行号）及计数 |
| `POST /delete` | `{"ids":[...]}` 或 `{"where":<expr>}`，软删除（幂等，返回 `changed`） |
| `POST /restore` | 同上，恢复（幂等） |
| `GET /stats` | 空间统计：每列/每值 raw 与 RLE 字节、字典字节、原始数据字节、存活掩码字节、压缩比 |

错误约定：400 请求/表达式非法（坏 JSON、未知列/算子、ids 越界、ids 与 where 同现）；
409 未加载数据集；404/405 路由错误；响应体均为 `{"ok":false,"error":...}`。

完整 curl 样例见 [`REQUESTS.md`](REQUESTS.md)。

## 关键语义（验收点对照）

1. **枚举列过滤**：`eq`/`in` 直接命中值位图。
2. **AND/OR/NOT**：位运算 `and/or/andNot`；`NOT` 在 `QueryEngine` 内实现为
   `alive.andNot(inner)`，并且顶层查询再与存活掩码求交（双保险）。
3. **删除掩码**：`alive` 位掩码，删除清位、恢复置位；按表达式删除的选择集在
   全数据集上求值（已删行被选中也是幂等 no-op），`changed` 只统计真实变化。
4. **行 ID 不随压缩变化**：`Bitmap` 定长，RLE 只记录“位值+游程长度”，解码严格
   按原下标回放；建索引时对每个值位图做编解码往返逐位断言。

## 空间统计口径（GET /stats）

- `rawBytes`：位图 `long[]` 占用（`ceil(rowCount/64)*8`，每值一张）；
- `rleBytes`：该位图 RLE 编码后的字节；
- `dictionaryUtf8Bytes`：列名 + 各类别值名的 UTF-8 字节；
- `rawDatasetBytes`：原始 CSV/数据字节；
- `aliveMaskRawBytes/aliveMaskRleBytes`：存活（删除反）掩码两种口径；
- `totalRawBytesInclMasks / totalRleBytesInclMasks`：索引 + 字典 + 两份掩码。

RLE 对**成块低基数列**压缩极好（5000 行实测 0.95%）；对**高基数唯一值列**单张
位图很小但总字节随基数线性增长；对**严格交替的最坏游程列**压缩反而膨胀
（实测 7.82×）。统计如实输出，不回避压缩失效场景。

## 测试与验收

`./test.sh`（约秒级，退出码 0/1），共 **232,913 条断言**：

- 位图基础运算与尾部掩码；RLE 随机/边界往返（含 0 长度空位图、word 边界、
  单点、单洞、交替、全 0/全 1）；CSV 解析与错误输入；
- **空全集（0 行）**：eq/and/or/not/alive/查询/统计全部返回空且长度为 0；
- **高基数列**：5000 个唯一值的体积与压缩统计；最坏游程列压缩率如实反映；
- **删除后补集**：`NOT(city=BJ)` 在删除/恢复不同阶段与手工行集一致，
  双重 NOT 等价存活内原谓词，任何表达式都不返回已删除行；
- **行 ID 稳定**：删除一半行后命中行仍以原始编号返回（有“空洞”，首个 BJ 命中
  是行号 3 而非重编号后的 0）；
- **随机布尔表达式 vs 行扫描**：400 行 × 含高基数 sku 列，4 个存活阶段
  （无删除 / 按 ID 删除 / 按表达式再删 / 部分恢复）各 600 条递归随机表达式，
  与独立的逐行扫描参考实现逐行 ID 比对，0 不一致；
- **HTTP 端到端**：真实启动 `HttpServer`，经 `java.net.http.HttpClient` 回环
  验证加载、查询、删除、恢复、limit、stats 与全部错误码。

实际运行输出见 [`RUN_LOG.md`](RUN_LOG.md)。

## 未完成项 / 已知限制（如实列出）

- **并发写隔离粒度**：所有写操作在服务实例上串行（`synchronized`），适合
  离线/中小数据量演示与对拍；没有做读写锁分离或增量更新，`/load` 是全量替换。
- **仅枚举等值查询**：没有数值范围（`>`, `<`, between）、文本 LIKE、连接/聚合、
  分页游标；`limit` 仅截断返回，不提供 offset。
- **压缩只用于统计与校验**：内存中常驻未压缩 `long[]`，RLE 编码只服务
  `/stats` 空间统计与“压缩不改行 ID”的往返校验，未做磁盘持久化/内存压缩驻留。
- **无持久化**：进程重启后需重新 `/load`（可用 `--autoload` 自动加载）。
- **无鉴权/TLS**：默认明文 HTTP、绑定所有网卡；`csvPath` 可读取服务进程权限
  内的本地文件，生产环境需自行加鉴权与路径白名单。
- **CSV 大小上限**：HTTP 请求体上限 256 MiB；超大文件建议用命令行 `--autoload`。
