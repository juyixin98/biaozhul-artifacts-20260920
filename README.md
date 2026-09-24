# 位置倒排短语检索服务（纯 JDK 后端）

基于**文档位置倒排索引（positional inverted index）**的短语检索 HTTP 服务。
纯后端、无界面；只用 JDK 标准库（`com.sun.net.httpserver.HttpServer`），
**零第三方依赖**，不需要 Maven/Gradle。

- 分词规则（固定）：按空白切分（空格/制表/换行等）、小写化；连续空白视为一个分隔符。
- 精确短语检索（短语必须在同一文档内、token 位置连续）。
- 布尔组合：`AND` / `OR` / `NOT`（大小写不敏感）、括号、隐式 AND。
- 文档插入 / 替换 / 删除；**替换或删除后旧位置全部清除，不残留**（替换为
  空文档同样清除全部位置，但文档本身仍存在并参与 `NOT`）。
- 空文档、纯空白文档、重复词短语、连续空格均为一等公民。

---

## 1. 依赖与环境

| 项目 | 说明 |
|------|------|
| JDK | **Java 17+**（验证环境：OpenJDK `17.0.20.1`，Ubuntu 24.04） |
| 第三方依赖 | **无**（锁定记录见 [`LOCKDEPS.md`](LOCKDEPS.md)） |
| 构建工具 | 无，直接用 `javac` |
| 测试框架 | 无，仓库内置约 50 行断言器 `test/phraseindex/Suite.java` |

使用的 Java 17 特性：`sealed` 接口、record、switch 表达式、`instanceof` 模式匹配。

## 2. 目录结构

```
.
├── README.md / LOCKDEPS.md
├── build.sh                 # javac 编译（主代码 + 测试）到 out/
├── test.sh                  # 编译并运行全部自动化测试
├── src/phraseindex/
│   ├── Tokenizer.java       # 固定分词：空白切分 + 小写
│   ├── Query.java           # 查询 AST（sealed：Phrase/And/Or/Not）
│   ├── QueryParser.java     # 递归下降解析器（含隐式 AND、括号、NOT）
│   ├── QueryParseException.java
│   ├── InvertedIndex.java   # 位置倒排索引（读写锁；替换/删除清旧位置）
│   ├── BruteForceJudge.java # 验收用：逐文档扫描判定器（oracle）
│   ├── Json.java            # 极简 JSON 读写（无依赖）
│   └── HttpServerApp.java   # JDK HttpServer + HTTP 路由（main 入口）
└── test/phraseindex/
    ├── Suite.java           # 迷你测试框架
    ├── TokenizerTest.java
    ├── QueryParserTest.java
    ├── InvertedIndexTest.java
    ├── DifferentialTest.java# 随机差分：索引 vs 逐文档扫描判定器
    ├── HttpE2ETest.java     # 真实启动 HTTP 服务做端到端测试
    └── AllTests.java        # 测试总入口
```

## 3. 启动命令

```bash
# 编译（只需 JDK，无任何下载）
./build.sh

# 启动服务（默认端口 8080，可传参覆盖）
java -cp out phraseindex.HttpServerApp         # http://localhost:8080
java -cp out phraseindex.HttpServerApp 8090    # 指定端口
```

停止：`Ctrl+C`（已注册 shutdown hook，会关闭 HttpServer）。

健康检查：

```bash
curl http://localhost:8080/health
# {"documents":0,"status":"ok"}
```

`GET /` 返回服务说明与端点列表。

## 4. HTTP 接口

| 方法 | 路径 | 请求体 | 说明 |
|------|------|--------|------|
| GET | `/health` | — | 健康检查 + 文档数 |
| PUT | `/documents/{id}` | **原始 UTF-8 文本** | 插入或整体替换文档；`{id}` 为非负整数 |
| POST | `/documents/bulk` | `{"docs":[{"id":1,"text":"..."}]}` | 批量插入/替换 |
| GET | `/documents/{id}` | — | 取文档原文；不存在返回 404 |
| DELETE | `/documents/{id}` | — | 删除文档及其全部位置；不存在返回 404 |
| POST | `/search` | `{"query":"...","includePositions":false}` | 检索 |

`/search` 响应：

```json
{"query":"\"go go\"","count":1,"docIds":[1],"positions":{"1":[0,1]}}
```

- `docIds`：命中文档 id，升序。
- `positions`：仅当顶层查询是一个短语且 `includePositions:true` 时返回，
  给出该短语在每个命中文档中的**起始 token 位置**（可出现多次，如重复词）。

错误响应统一为 `{"error": "...", "status": 400}`，状态码语义常规
（400 参数/语法错误，404 文档或路由不存在，405 方法不允许）。

## 5. 查询语法

```
orExpr  := andExpr (OR andExpr)*
andExpr := unary [(AND | 隐式) unary]*      # 相邻两个原子之间即隐式 AND
unary   := NOT unary | atom
atom    := 短语 | '(' orExpr ')'
短语    := "双引号包裹、空格分隔的多个词" | 裸单词
```

- 运算优先级：`OR` < `AND`（含隐式）< `NOT` < 原子；括号可改变优先级。
- 裸单词等价于单词短语；短语内同样按固定规则分词（连续空格坍缩、小写）。
- 关键词大小写不敏感（`and`/`And` 均可）；要把关键词当普通词检索，加引号：`"and"`。
- 空短语 `""` 合法但不命中任何文档；整体为空的查询串返回 400。

## 6. 请求样例（实测）

以下输出为本机真实运行结果（`bash` 下单引号包裹 JSON，内部双引号无需转义）：

```bash
B=http://localhost:8080

# 索引：重复词 / 孤立词（跨文档边界）/ 连续空格 / 空文档
curl -s -X PUT --data-binary 'go go go'          $B/documents/1
curl -s -X PUT --data-binary 'go'                $B/documents/2
curl -s -X PUT --data-binary '  hello   world  ' $B/documents/3
curl -s -X PUT --data-binary ''                  $B/documents/4
```

重复词短语 + 命中位置（"go go" 在 "go go go" 中起始位置为 0、1）：

```bash
curl -s -X POST -H 'Content-Type: application/json' \
  -d '{"query":"\"go go\"","includePositions":true}' $B/search
# {"query":"\"go go\"","count":1,"docIds":[1],"positions":{"1":[0,1]}}
```

跨文档不误匹配（doc2 的孤立 `go` 不能与 doc1 串成短语）：

```bash
curl -s -X POST -H 'Content-Type: application/json' \
  -d '{"query":"go AND NOT \"go go\""}' $B/search
# {"query":"go AND NOT \"go go\"","count":1,"docIds":[2]}
```

连续空格坍缩；布尔/括号；空文档参与 NOT：

```bash
curl -s -X POST -H 'Content-Type: application/json' -d '{"query":"\"hello world\""}' $B/search
# {"query":"\"hello world\"","count":1,"docIds":[3]}

curl -s -X POST -H 'Content-Type: application/json' -d '{"query":"NOT (go OR hello)"}' $B/search
# {"query":"NOT (go OR hello)","count":1,"docIds":[4]}
```

文档替换（`replaced`），旧位置不残留：

```bash
curl -s -X PUT --data-binary 'zebra' $B/documents/1
# {"id":1,"tokens":1,"action":"replaced"}

curl -s -X POST -H 'Content-Type: application/json' -d '{"query":"go"}' $B/search
# {"query":"go","count":1,"docIds":[2]}      # 只剩 doc2，doc1 的 go 已清除
```

删除、批量、错误：

```bash
curl -s -X DELETE $B/documents/3
curl -s -X POST -H 'Content-Type: application/json' \
  -d '{"docs":[{"id":10,"text":"quick brown fox"},{"id":11,"text":"lazy brown dog"}]}' \
  $B/documents/bulk
curl -s -X POST -H 'Content-Type: application/json' -d '{"query":"\"brown fox\" OR dog"}' $B/search
# {"query":"\"brown fox\" OR dog","count":2,"docIds":[10,11]}

curl -s -X POST -H 'Content-Type: application/json' -d '{"query":"a AND"}' $B/search
# {"error":"query parse error: expected a term or phrase (at position 5)","status":400}
```

## 7. 自动化测试

```bash
./test.sh        # = build.sh + java -cp out phraseindex.AllTests
```

测试套件（共 **33** 个用例，全部通过；退出码 0）：

| 套件 | 用例数 | 内容 |
|------|-------|------|
| Tokenizer | 6 | null/空串、纯空白、连续空格坍缩、小写、重复词保留 |
| QueryParser | 13 | 重复词短语、空短语、隐式 AND、优先级、括号、NOT、引号关键词、各类语法错误 |
| InvertedIndex | 11 | **重复词短语及全部起始位置**、**连续空格**、**空/纯空白文档**、**跨文档误匹配防护**、替换无残留（含替换为空）、共享词项替换互不影响、删除清理、布尔语义 |
| Differential | 1 | **300 轮随机增/替/删 × 每轮 53 个查询（13 固定 + 40 随机 AST），索引结果逐文档比对 `BruteForceJudge`**；同时重建期望 posting 表逐字段比对（term 集合/文档集合/位置列表），从内部状态层面证明无旧位置残留 |
| HttpE2E | 2 | 真实启动 HTTP 服务（临时端口 + JDK `HttpClient`）：完整生命周期、短语、位置、布尔、空文档、替换、bulk、删除、400/404、UTF-8 |

### 验收点对应

| 验收要求 | 覆盖位置 |
|----------|----------|
| 重复词短语（多起始位置） | InvertedIndexTest 第 1 用例、HttpE2E、Differential 固定查询 `"go go go"` / `"ha ha"` |
| 连续空格 | TokenizerTest、InvertedIndexTest「double spaces」、Differential 随机文档生成（1–3 个空白、含 `\t\n`）、HttpE2E doc3 |
| 空文档 | InvertedIndexTest 两个用例、Differential 强制空/纯空白替换、HttpE2E doc4/doc5 |
| 跨文档误匹配 | InvertedIndexTest「phrase cannot match across document boundaries」、HttpE2E doc1+doc2、Differential 多文档随机库 |
| 逐文档扫描判定器比对 | `BruteForceJudge`（滑窗扫描 + 同构布尔求值）被 `DifferentialTest` 对每条查询调用比对，并逐位置核对 |

## 8. 实测结果（如实记录）

- `./test.sh`：5 个套件 33 个用例全部通过，JVM 正常退出（exit 0）。
  开发过程中测试真实发现并修复了一个索引 bug：**文档替换/删除导致某词项在所有
  文档中消失时，词项的空 posting 键残留在索引中**（差分测试的 posting 表比对
  抓到）；已修复为删除空词项键，并补了对应断言。
- 按第 6 节手工启动服务逐条执行 curl，响应与记录一致。

## 9. 已知限制 / 未完成项

- 纯内存索引：重启数据丢失，无持久化。
- 无并发写冲突控制之外的多版本/事务语义；读写锁保证单实例线程安全，
  未做分布式/多实例。
- 查询语言无短语邻近量词（如 NEAR/k）、无字段检索、无分页；结果全量返回。
- 未做认证鉴权、速率限制与大请求体保护（本地/内网练习用途）。
- 仅在 Linux x86_64 + OpenJDK 17 上验证；代码为标准 Java 17，预期可在
  Windows/macOS 的 17+ JDK 上直接运行，但未实测。
