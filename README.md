# BM25 稳定分页（bm25-stable-pagination）

纯后端本地文本检索服务：**Java 实现 BM25 检索 + JSON HTTP 服务**。不调用任何外部搜索服务或大模型，
语料为服务启动时装载的**自建合成语料**（21 篇，含空文档、仅标点文档、重复词与同分文档组）。

核心目标是**稳定分页**：

- BM25 参数与分词规则**固定**，结果完全可复现；
- 分页**绑定索引快照版本**：第一页游标记录快照版本，后续页始终复用同一快照，
  期间语料的新增 / 修改 / 删除**不会影响已经开始的分页**；
- 排序固定为 **BM25 分数降序，分数相同按文档 ID 字典序升序**，保证同分有确定次序；
- 采用**键集（keyset）游标**（记录上页末条的 `(score, docId)`），连续分页**不漏、不重**；
- 快照有保留上限（最近 8 个），旧游标指向被淘汰的快照时返回 **HTTP 410 `SNAPSHOT_EXPIRED`**，
  客户端应丢弃游标、从第一页重新开始。

## 技术栈与依赖

- Java 21（OpenJDK 21 实测）；
- **零外部依赖**：HTTP 用 JDK 内置 `com.sun.net.httpserver.HttpServer`，JSON 为项目内手写的
  迷你解析/序列化器（`Json.java`），不需要 Maven/Gradle，`javac` 直接构建。

## 目录结构

```
.
├── README.md
├── scripts/
│   ├── build.sh          # javac 编译到 out/
│   ├── test.sh           # 构建并运行全部自动化测试
│   ├── run.sh            # 构建并启动服务（默认 8080）
│   └── demo.sh           # 端到端演示（分页/同分/快照隔离/410/错误处理）
├── src/main/java/com/bm25stable/
│   ├── Main.java            # 入口：装载合成语料、启动服务
│   ├── HttpSearchServer.java# JSON HTTP 路由
│   ├── SearchEngine.java    # 引擎：快照管理 + 检索 + 稳定分页
│   ├── IndexSnapshot.java   # 不可变索引快照（倒排索引、文档长度、avgdl）
│   ├── BM25.java            # BM25 打分（k1=1.2, b=0.75 固定）
│   ├── Tokenizer.java       # 固定分词规则
│   ├── SearchCursor.java    # 游标：base64url(JSON)，绑定快照版本
│   ├── SearchResult/SearchHit/Document/SearchException.java
│   ├── SyntheticCorpus.java # 自建合成语料
│   └── Json.java            # 零依赖迷你 JSON
├── src/test/java/com/bm25stable/   # 31 个自动化测试（自带迷你测试框架）
└── examples/
    ├── requests.md         # curl 请求样例
    ├── test-output.txt     # 自动化测试的实际运行记录
    └── demo-output.txt     # demo.sh 的实际运行记录
```

## 快速开始

```bash
./scripts/build.sh          # 编译
./scripts/run.sh            # 启动，默认 http://localhost:8080（可用 PORT 环境变量或参数改端口）
# 另一个终端：
curl -s 'http://localhost:8080/search?q=apple&pageSize=2'
```

## HTTP 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 存活探针，返回当前版本与文档数 |
| GET | `/search?q=&pageSize=&cursor=` | 检索；无 cursor 为第一页 |
| POST | `/documents` | 新增/覆盖：`{"id":"x","text":"..."}` |
| POST | `/documents/bulk` | 批量：`{"documents":[{"id","text"}, ...]}` |
| DELETE | `/documents/{id}` | 删除文档 |
| GET | `/snapshots` | 当前保留的快照版本列表 |

检索响应示例：

```json
{
  "snapshotVersion": 1,
  "totalHits": 5,
  "offset": 0,
  "pageSize": 2,
  "hits": [
    {"docId": "case-1", "score": 2.2667786175068487, "text": "Apple APPLE aPpLe"},
    {"docId": "doc-01", "score": 2.2667786175068487, "text": "apple apple apple"}
  ],
  "nextCursor": "eyJxIjoiYXBwbGUi...",
  "hasMore": true
}
```

## 设计说明

### BM25（参数固定）

- `k1 = 1.2`，`b = 0.75`，写死在 `BM25.java`，不通过配置暴露；
- IDF 使用 +1 平滑变体（保证非负）：
  `idf(t) = ln(1 + (N - df(t) + 0.5) / (df(t) + 0.5))`；
- 文档得分：`Σ idf(t) · tf·(k1+1) / (tf + k1·(1 - b + b·dl/avgdl))`；
- 查询词**去重后**求和（`apple apple apple` 与 `apple` 等价）；
- 分数为 `double`，同一快照下同样输入必然产生同样结果，键集定位使用精确相等比较，
  不存在“边界分数漂移导致跨页重复/丢失”的问题。

### 分词（规则固定）

全文转小写后按连续 `[a-z0-9]+` 切词，其余字符一律是分隔符；不做词干提取、不去停用词。
空文档、仅标点文档产生空词元列表，永远不会出现在命中里。

### 快照与稳定分页

1. 每次语料发生**实质性**变化（内容相同的重复写入、删除不存在的文档不产生新版本），
   引擎重建一个**不可变** `IndexSnapshot`，版本号单调递增；
2. 第一页请求不带游标，绑定**最新**快照；游标（`base64url(JSON)`）内含
   快照版本 `v`、规范化查询 `q`、pageSize `ps`、上页末条 `(s, id)` 与累计偏移 `off`；
3. 后续页凭游标取回同一快照打分排序，并从键 `(s, id)` 之后继续切片；
4. 最近 8 个快照保存在内存中（LinkedHashMap FIFO 淘汰）。旧快照被淘汰后使用旧游标 →
   **410 SNAPSHOT_EXPIRED**；
5. 游标与请求不一致（查询词变化、pageSize 变化）→ **400 CURSOR_MISMATCH**；
   游标损坏 → **400 INVALID_CURSOR**。

### 错误格式

```json
{"error": {"code": "BAD_REQUEST", "message": "..."}}
```

| HTTP | code | 触发条件 |
|---|---|---|
| 400 | `BAD_REQUEST` | 缺 q、pageSize 越界（1..100）、请求体非法 |
| 400 | `INVALID_CURSOR` | 游标不是合法 base64/JSON 或缺字段 |
| 400 | `CURSOR_MISMATCH` | 游标与请求的 q / pageSize 不一致 |
| 404 | `NOT_FOUND` | 未知路由 |
| 405 | `METHOD_NOT_ALLOWED` | 路由存在但方法不允许 |
| 410 | `SNAPSHOT_EXPIRED` | 游标绑定的快照已被淘汰，需重新从第一页开始 |

## 验收点对应

| 验收要求 | 覆盖位置 |
|---|---|
| 空文档 | `TokenizerTest`、`BM25Test.emptyDocumentsNeverMatch`、合成语料 `empty-1`/`punct-1` |
| 重复词（文档内/查询内） | `TokenizerTest.repeatedTokensArePreserved`、`BM25Test.repeatedTermIn*` |
| 同分结果 | `BM25Test.equalScoresBrokenByDocIdAscending`、合成语料 `dup-1..3`、`case-1`/`doc-01` |
| 连续分页无漏重 | `PaginationTest.consecutivePagesHaveNoGapsOrDuplicates`、`pageSequenceMatchesSingleBigPage`、`HttpApiTest.endToEndPaginationNoGapsNoDuplicates` |
| 同分跨页稳定 | `PaginationTest.tiedScoresAreStablyOrderedAcrossPageBoundary` |
| 更新不影响旧游标 | `SnapshotTest.updatesDoNotAffectInFlightPagination`、`HttpApiTest.snapshotIsolationOverHttp` |
| 快照过期错误 | `SnapshotTest.expiredSnapshotYields410Semantics`、`HttpApiTest.expiredCursorReturns410`（断言 HTTP 410 与错误码） |
| 参数/游标错误 | `PaginationTest.malformedCursorRejected/cursorBindsQueryAndPageSize`、`HttpApiTest.errorCasesOverHttp` |

## 自动化测试

```bash
./scripts/test.sh
```

自带零依赖迷你测试框架（`TestFramework`，反射扫描 `@TestCase` 方法）。
共 **31** 个用例，分 5 组：分词、BM25 打分、分页、快照隔离、HTTP 端到端（随机端口启动真实服务）。

### 实际运行记录（如实记录）

最新一次完整运行：`./scripts/test.sh` → **31 通过 / 0 失败**，输出见
[`examples/test-output.txt`](examples/test-output.txt)；端到端演示输出见
[`examples/demo-output.txt`](examples/demo-output.txt)。

开发过程中曾出现 **2 个失败项，均已修复并复测通过**（未绕过、未删除用例）：

1. `BM25Test.scoresMatchHandComputedReference`（d2 分数不符）——
   原因是**测试基准值本身算错**：手写 Python 对照时误把 `d2="a b"` 的文档长度 dl 当作 1，
   实际分词长度为 2，引擎结果 `0.19856803215183175` 正确。用 Python 以正确 dl=2 重算确认后，
   修正测试常量与注释，通过；
2. `HttpApiTest.errorCasesOverHttp`（未知路由解析报错）——
   原因是**实现缺陷**：未注册路径由 JDK HttpServer 内置处理器返回 HTML 404，不是 JSON。
   修复方式是增加 `/` 兜底上下文统一返回 JSON `NOT_FOUND`，复测通过。

（另修复了两个构建层面的小问题：Javadoc 注释中的 `\uXXXX` 被 javac 当作 Unicode 转义；
`@TestCase` 注解最初缺少 `@Retention(RUNTIME)` 导致测试方法未被发现。）

## 运行环境

- OpenJDK 21（实测 `21.0.12`，Linux x86_64）；
- 无需网络、无需安装构建工具。
