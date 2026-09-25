# BM25 稳定分页服务（纯后端）

一个零外部依赖的 Java 本地文本检索服务：实现固定参数的 BM25 检索，并通过
**绑定索引快照的键集游标（keyset cursor）**保证翻页稳定性——索引更新后旧游标
仍然读取它开始时的那份不可变快照，连续分页不漏、不重；快照被驱逐后返回明确的
过期错误。不调用任何外部搜索服务或大模型，语料为程序内置的确定性合成语料。

- 语言/运行时：Java 21（仅用 JDK 标准库，含内置 `com.sun.net.httpserver.HttpServer`）
- 构建：无需 Maven/Gradle，`javac` 直接编译；三个 shell 脚本即可构建、测试、运行
- 数据：全部在内存中，启动时由 `SyntheticCorpus` 生成 69 篇文档

## 目录结构

```
src/main/java/com/bm25pager/
├── Main.java                     # 入口：装载语料、启动 HTTP 服务
├── corpus/SyntheticCorpus.java   # 确定性合成语料（含空文档/同分/重复词构造）
├── text/Tokenizer.java           # 固定分词规则（查询与索引共用）
├── search/
│   ├── Bm25.java                 # 固定参数 BM25 打分（k1=1.2, b=0.75）
│   ├── Cursor.java               # 自描述翻页游标（JSON -> URL-safe Base64）
│   ├── SearchService.java        # 检索 + 键集分页 + 片段
│   ├── SearchResult.java / Hit.java
│   ├── InvalidCursorException.java
├── index/
│   ├── IndexSnapshot.java        # 不可变快照：倒排表 + 长度统计 + 确定性排名
│   ├── IndexManager.java         # 工作区、commit 版本、快照保留/驱逐
│   ├── Postings.java
│   └── SnapshotExpiredException.java
├── json/Json.java                # 零依赖 JSON 解析/序列化
├── api/ApiServer.java            # JDK HttpServer 的 JSON REST 接口
└── model/Document.java
src/test/java/com/bm25pager/Tests.java   # 651 条断言的零依赖测试（含真实 HTTP）
examples/requests.sh                     # curl 请求样例
examples/out/                            # 样例的真实响应留档
build.sh / run.sh / run-tests.sh
```

## 快速开始

```bash
# 1) 编译
./build.sh

# 2) 运行自动化测试（编译主代码+测试并执行，退出码 0 表示全过）
./run-tests.sh

# 3) 启动服务（默认端口 8080）
./run.sh
# 可选环境变量：BM25_PORT=9090 BM25_RETAIN_SNAPSHOTS=3 ./run.sh

# 4) 发请求
curl -s http://127.0.0.1:8080/health
curl -s -X POST http://127.0.0.1:8080/search \
  -H 'Content-Type: application/json' \
  -d '{"query":"banana","pageSize":2}'
```

更多可直接执行的请求见 `examples/requests.sh`；真实响应留档在 `examples/out/`。
实际执行命令与结果的如实记录见 [RUN_LOG.md](RUN_LOG.md)。

## 检索与排序约定（全部固定，不可经 API 修改）

**分词规则**（`Tokenizer`）：

- 连续的 ASCII 字母数字 `[A-Za-z0-9]+` 为一个 token（数字不切分，`token10` 是一个词）；
- 其余字符（含中文等 CJK 字符、标点、空白）一律作为分隔符；
- token 统一转小写（`Locale.ROOT`），不做停用词、不做词干化；
- 查询与索引使用同一套规则。

**BM25 参数与公式**（`Bm25`）：

- `k1 = 1.2`，`b = 0.75`；
- IDF 采用 Lucene 经典形式（恒非负）：
  `idf(t) = ln(1 + (N - n + 0.5) / (n + 0.5))`，`N` 为快照文档数，`n` 为含词项 `t` 的文档数；
- `score(D,Q) = Σ idf(t) · tf·(k1+1) / (tf + k1·(1 - b + b·|D|/avgdl))`；
- 查询词项先去重；文档至少包含一个查询词项（总分 > 0）才进入排名；
- **排序：分数降序；分数相同时按 `docId` 字典序（`String#compareTo`）升序**，
  完全确定，与 JVM/平台无关（不依赖 `HashMap` 遍历顺序）。

**空文档/重复词的行为**：

- 空正文或仅含标点的文档长度为 0，对任何查询都不命中、不产生 NaN（合成语料中的
  `empty-001`、`empty-002`）；
- 词项重复通过 BM25 的词频饱和处理：重复越多得分越高但边际递减，不是线性放大
  （`repeat-001` 30 次重复 vs `repeat-002` 2 次，测试中核对了饱和比 < 2 倍的同长度对照）。

## 分页与快照模型

1. **第一页** `POST /search {"query":..., "pageSize":...}` 在**当前版本**快照上计算完整排名，
   返回前 N 条与 `nextCursor`。游标内含：快照版本 `v`、查询词项、页大小、页码、
   上一页最后一条的 `(lastScore, lastId)`，整体为 JSON 的 URL-safe Base64（无填充）。
2. **后续页** `POST /search/continue {"cursor":...}`：版本、查询、页大小均**以游标为准**
   （忽略请求里新传的 query/pageSize，避免同一次翻页参数漂移），服务端在游标绑定的
   **同一份不可变快照**上重算排名，从 `(lastScore, lastId)` 的下一条开始切片。
3. 因为快照不可变、边界用精确 double 分数 + docId 定位（不是 offset），
   索引的增删改**不影响进行中的翻页**，无漏读、无重复。
4. 快照默认保留最近 5 个版本（`BM25_RETAIN_SNAPSHOTS` 可调，最少保留 1 个）。
   旧版本被驱逐后，仍引用它的游标翻页返回 **HTTP 410 `SNAPSHOT_EXPIRED`**，
   并带回 `requestedVersion/currentVersion`；调用方应丢弃游标、重新请求第一页。
   游标字符串本身损坏（非 base64、字段缺失/类型错）返回 **HTTP 400 `INVALID_CURSOR`**。
5. 新增/更新/删除文档默认自动 `commit` 产生新版本（见接口表）；也可批量修改后
   `POST /admin/commit` 一次生效，或 `POST /admin/compact?keep=1` 主动驱逐旧版本。

## HTTP 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 健康检查，返回当前版本 |
| POST | `/search` | 首页检索；body `{"query":"...","pageSize":10}`（pageSize 可省，缺省 10，裁剪到 1–100；query 允许空串，返回空结果） |
| POST | `/search/continue` | 翻页；body `{"cursor":"..."}`；410 快照过期 / 400 游标损坏 |
| POST | `/documents/upsert` | 新增或更新；`{"docId","content","metadata"?}`，自动 commit 新版本 |
| DELETE | `/documents/{docId}` | 删除（不存在时 `found:false`），自动 commit |
| POST | `/admin/commit` | 显式发布新快照；无修改时返回当前版本 |
| POST | `/admin/compact?keep=1` | 驱逐旧快照，仅保留最近 keep 个 |
| GET | `/admin/status` | 当前版本、保留版本列表、文档/词项数、长度统计 |

命中对象字段：`docId`、`score`（double 原始值）、`snippet`（空白归一化、截断 120 字符）、
`metadata`。分页响应字段：`version`、`page`（0 基）、`pageSize`、`totalHits`、
`hits`、`nextCursor`（末页为 `null`）、`hasMore`。

错误响应统一为 `{"error":"CODE","message":"..."}`，`SNAPSHOT_EXPIRED` 额外带
`requestedVersion` 与 `currentVersion`。

## 合成语料

由 `SyntheticCorpus.create()` 在每次启动时确定性生成，共 69 篇：

- `empty-001`（空串）、`empty-002`（仅空白标点）；
- `tie-001`/`tie-002`（对 `banana` 完全同分）、`tie-003`（banana 词频更高）；
- `repeat-001`（30 次重复）、`repeat-002`（2 次重复）；
- `numeric-1`（数字混排 `v2`、`token10`）、`cjk-1`（中文分隔符 + 英文词）；
- `common-0001..0060`：每篇都含 `data`，词频/长度按编号确定性变化，用于大结果集分页。

## 测试

`src/test/java/com/bm25pager/Tests.java` 是不依赖 JUnit 的独立套件（`main` 运行，
断言失败时退出码 1），共 **651 条断言**，分组覆盖：

1. 分词规则（大小写、数字、CJK 分隔、空输入）；
2. BM25 手算数值（N=3 封闭语料，IDF/长度归一化按公式逐项核对到 1e-12）；
3. 空文档、仅标点文档、空查询、未知词；
4. 重复词的词频饱和与长度归一化；
5. 同分时 docId 升序；
6. 连续分页在页大小 1/2/7/13/100 及多词/重复查询词下**不漏不重**、顺序与基线一致；
7. 游标绑定快照：更新（增+删）后旧游标仍读旧版本、新旧游标可交错；
8. 快照过期：版本被保留窗口驱逐或 compact 后得到 `SNAPSHOT_EXPIRED`（核对版本号）；
9. 损坏/缺字段游标得到 `INVALID_CURSOR`；
10. 真实 HTTP 端到端：启动临时端口的服务，用 JDK `HttpClient` 走完整链路。

```bash
./run-tests.sh     # 编译并运行，末尾打印 "N passed, 0 failed"
```

## 设计边界与说明

- 全内存实现，重启后语料重置为内置合成语料（本任务不要求持久化）；
- 快照保留窗口是有界的：驱逐是“过期错误”这一验收点所必需的语义，不是缺陷；
- 游标是不透明但可解码的 Base64，客户端不应构造或修改它，服务端会做完整校验；
- 无前端，无第三方依赖。
