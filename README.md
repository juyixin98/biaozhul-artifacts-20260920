# position-diff — 带位置的行级差异算法（纯后端）

零外部依赖的 Java 后端项目：对文本做**行级 Myers 差异**，生成**可应用的编辑序列**
（每个操作带绝对行号位置），把**换行形式（LF / CRLF）与末尾换行当作内容**处理；
支持**预算限制**并在超限时**显式返回降级结果**；同时提供一个对**自建合成语料**做
本地检索的库，以及一个 **JSON HTTP 服务**。不调用任何外部搜索服务或大模型，无前端。

- JDK：21（语法也兼容 17+；只用 `javac`/`jar`/`java`，不需要 Maven/Gradle）
- 依赖：无第三方库。HTTP 使用 JDK 自带 `com.sun.net.httpserver`，JSON 为自写的
  小型解析器/序列化器，测试为自写的迷你测试框架。

## 目录结构

```
src/com/example/positiondiff/
  model/      Line, Eol, Hunk
  text/       LineSplitter            行切分（CRLF 作为一个终止符；末尾换行入模）
  json/       JsonParser, Json        零依赖 JSON
  diff/       Myers                   Myers O(ND) 前向搜索 + 回溯
              Budget                  搜索预算（maxNodes / maxD）
              EditOp                  带 oldIndex/newIndex 的原子编辑
              DiffResult              最优 / 降级结果（不谎报最短）
              DiffEngine              门面：切行→Myers→标注位置→兜底→apply
              Hunks                   hunk 分组 + unified-diff 渲染
  search/     Corpus                  自建合成语料（中英混合、含重复行）
              Tokenizer               拉丁词 + CJK 单字/邻接二元
              SearchIndex             内存倒排 + tf-idf 评分
  server/     Server, Api             localhost-only JSON HTTP 服务
  Main.java                           CLI 入口
test/         TestRunner, Tests       自动化测试（含 2000 组随机往返）
examples/     请求样例
scripts/      build.sh / test.sh / demo.sh
```

## 构建与测试（实际运行）

```bash
scripts/build.sh        # javac 编译 + jar 打包
scripts/test.sh         # 运行全部自动化测试
scripts/demo.sh [port]  # 起服务，用 examples/ 逐个打端点，再跑 CLI
```

本次环境中的实际结果（JDK 21，Linux x86_64）：

```
$ scripts/build.sh
[1/3] compiling main sources
[2/3] compiling tests
[3/3] packaging jar
OK -> build/jar/position-diff.jar

$ scripts/test.sh
...
tests: 43, passed: 43, failed: 0
    (随机用例中降级路径被触发 52 次，全部 apply 精确恢复目标)
```

测试覆盖：行切分（空串、`"\n"`、CRLF、LF/CRLF 混用、孤立 CR、无末尾换行）、
手工差异、**全重复行**、**空文件**、**CRLF 与末尾换行作为内容**、
最优结果与独立 O(NM) LCS 参考实现逐一比对（300 组随机）、
预算降级（maxNodes=0/小预算/maxD/50 组随机困难用例）、非法脚本拒绝、
hunk 范围、JSON 解析、本地检索，以及 **2000 组随机短文本的
diff→apply 往返**（其中约 1/37 强制走降级路径）。

## 核心语义

### 行与换行即内容

- `"\r\n"` 整体识别为一个 CRLF 行终止符；`'\n'` 为 LF；孤立 `'\r'` 视为普通字符。
- 每个逻辑行 = `{text, eol}`，`eol ∈ {LF, CRLF, NONE}`。
- `"a"` 是一行 `("a", NONE)`，`"a\n"` 是一行 `("a", LF)` —— 二者不同；
  同文本的 LF 行与 CRLF 行也不同。空文件是零个逻辑行，`"\n"` 是一个空的 LF 行。

### 带位置的编辑序列

`EditOp` 三种原子操作（位置均为 0 基）：

| type   | 含义 | oldIndex / newIndex |
|---|---|---|
| `equal` | 保留旧行 | 该行在旧/新文件中的绝对行号 |
| `delete` | 删除旧行 | oldIndex=被删行；newIndex=此刻新文件游标 |
| `insert` | 插入新行 | newIndex=插入后的新行号；oldIndex=已消费的旧行数（插入点位于旧行 oldIndex−1 与 oldIndex 之间） |

`equal`/`delete` 的 `oldIndex` 严格递增、恰好覆盖每个旧行一次，因此 apply 只需
按序校验/消费旧行即可，无需隐式计数器；插入位置也显式携带。

### 预算与显式降级（不谎报最短）

`Budget{maxNodes, maxD}`（任一为 `-1` 表示不限）：

- 预算内到达终点：`optimal=true, degraded=false`，并给出 `shortestDistance`
  （Myers 证明的最小编辑距离）。
- 预算耗尽：`optimal=false, degraded=true`，`shortestDistance=null`，
  `degradedReason` 说明原因；改用**精确公共前缀/后缀 + 中间整体删/插**的兜底脚本。
  该脚本**保证可应用且逐字节恢复目标**，但**绝不声称最短**——降级脚本的
  `editDistance` 只是其自身长度，不与最短距离混淆。
- `maxNodes=0` 仍允许 d=0 的平凡情况（两个文件完全相同）立即成功。

## CLI

```bash
java -jar build/jar/position-diff.jar serve [port]            # HTTP 服务，默认 8080
java -jar build/jar/position-diff.jar diff --file A B [--max-nodes N]
java -jar build/jar/position-diff.jar diff -                  # JSON 请求走 stdin
java -jar build/jar/position-diff.jar apply                   # {oldText, ops} 走 stdin
java -jar build/jar/position-diff.jar search QUERY [limit]
```

CLI 实测片段：

```
$ java -jar build/jar/position-diff.jar diff --file /tmp/c.txt /tmp/d.txt   # "a\r\nb" -> "a\r\nB\n"
optimal=true, editDistance=2
ops: equal {a,CRLF}, delete {b,NONE}, insert {B,LF}

# 困难排列 + maxNodes=2
optimal=False degraded=True
reason: budget exhausted after 3 search node(s); Myers had reached edit-distance layer d=1
        without proving a shortest script; fallback prefix/suffix script used
editDistance: 24   shortestDistance: null
fallback apply 精确恢复目标: True
stderr: NOTE: degraded result - script is valid but NOT proven shortest
```

## HTTP JSON 服务（仅绑定 127.0.0.1）

| 方法/路径 | 请求体 | 说明 |
|---|---|---|
| `POST /api/diff` | `{oldText, newText, budget?, context?}` | 文本可传字符串，或 `[{text,eol},...]` 行数组 |
| `POST /api/apply` | `{oldText, ops:[...]}` | 应用编辑序列，返回 `newText`；脚本非法返回 400 |
| `POST /api/search` | `{query, limit?}` | 在合成语料上本地检索 |
| `GET  /api/corpus` | — | 列出合成语料 |
| `GET  /health` | — | 健康检查 |

`/api/diff` 响应关键字段：`optimal`、`degraded`、`degradedReason?`、
`stats{editDistance, shortestDistance, insertions, deletions, equals, nodesUsed, ...}`、
`ops[]`（带位置与每行 `{text,eol}`）、`hunks[]`、`unifiedDiff`。

实测请求样例：

```bash
curl -s -X POST localhost:18080/api/diff \
  -H 'Content-Type: application/json' \
  -d '{"oldText":"a\nb\n","newText":"a\nB\n","budget":{"maxNodes":100000}}'

curl -s -X POST localhost:18080/api/apply \
  -H 'Content-Type: application/json' \
  --data @examples/apply-request.json
```

实测响应片段：

```json
{"optimal": false, "degraded": true,
 "degradedReason": "budget exhausted after 2 search node(s); ... fallback prefix/suffix script used",
 "stats": {"editDistance": 12, "shortestDistance": null, ...}}
```

非法输入（如 `not json`）返回 HTTP 400 与 `{error,status}`；用 GET 打 POST 端点
返回 405。

## 合成语料与本地检索

语料为手工编造的 6 篇文档（`Corpus.java`），主题围绕一个虚构项目，
中英混合且刻意包含重复行（如 `same line × 3`）。检索为进程内倒排索引，
tf-idf 变体评分，返回命中文档、分数、命中词数与 1 基行号；
中文按 CJK 单字 + 连续 CJK 片段内的邻接二元索引，无需分词器。

## 已知边界 / 如实说明

- 降级结果保证正确与可应用，但其编辑序列长度**不是**最短（响应中
  `shortestDistance` 恒为 `null`，并有 `degradedReason`）。
- 随机测试用“行文本小字母表 + 独立随机行尾”生成，制造大量匹配/重复与
  LF/CRLF/无末尾换行混合；它不是对所有可能输入的形式化证明，最优性的独立校验
  由 O(NM) LCS 参考实现在随机集上完成。
- 行级差异不对超长行内部做字符级对比；极极大输入受 JVM 内存限制（Myers 为
  O(N+M) 空间 / O(ND) 时间，预算可主动截断搜索）。
- 服务只监听 loopback，无鉴权、无 TLS（定位为本地库/本地服务）。
