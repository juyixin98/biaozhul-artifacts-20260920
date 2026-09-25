# 布尔检索查询计划（boolsearch）

纯后端 Java 项目：本地文本布尔检索库 + JSON HTTP 服务。零外部依赖（仅 JDK 21），
不调用任何外部搜索服务或大模型，输入为自建合成语料。

## 功能

- **查询解析**：`AND` / `OR` / `NOT` / 括号，优先级 `NOT > AND > OR`，关键字大小写不敏感；
  解析错误抛出带**字符位置**（0 基偏移，EOF 为串长）的异常。
- **NOT 语义**：相对**固定文档全集**（索引当前存活文档集合）求补，而非相对某个子集。
- **查询计划优化**：AND 节点求交前按各子结果集（倒排表）大小**升序**排列，小表驱动大表，
  中间结果为空即短路；`optimize=false` 时按书写顺序朴素求交。两种计划结果必然一致（测试保证）。
- **索引**：倒排索引（term → 有序 docId 集合），支持新增 / 覆盖 / 删除文档，
  删除后全集与倒排表同步收缩。
- **JSON 服务**：JDK 内置 `com.sun.net.httpserver` + 手写迷你 JSON 解析器（错误同样带位置）。

## 目录结构

```
src/main/java/boolsearch/
  Tokenizer.java            文档分词（小写化、字母数字切分）
  InvertedIndex.java        倒排索引 + 文档全集（增删、df）
  Corpus.java               合成语料生成（固定词表 + 种子，可复现）
  Main.java                 CLI 入口：serve / query / demo
  query/  Lexer, Parser, Node, ParseException   查询词法/语法/AST/错误位置
  eval/   Evaluator                             朴素与优化两种求值计划
  json/   Json                                  迷你 JSON 解析/序列化
  server/ SearchServer                          JSON HTTP 服务
src/test/java/boolsearch/   自动化测试（自写迷你框架，TestRunner 为入口）
build.sh                    编译（仅需 javac）
run-tests.sh                运行全部测试
examples/requests.sh        API 请求样例（curl）
```

## 构建与运行

```bash
./build.sh          # 编译到 out/main 与 out/test
./run-tests.sh      # 运行全部自动化测试（24 项）
```

启动服务（预载 100 篇合成文档）：

```bash
java -cp out/main boolsearch.Main serve --port 18099 --docs 100 --seed 42
```

CLI 单次查询 / 演示：

```bash
java -cp out/main boolsearch.Main query "(apple OR cherry) AND NOT grape" --docs 100
java -cp out/main boolsearch.Main demo
```

## HTTP API

| 方法 | 路径 | 请求体 | 说明 |
|---|---|---|---|
| GET | `/health` | — | 健康检查 |
| POST | `/documents` | `{"id":1,"text":"apple banana"}` | 新增/覆盖文档 |
| DELETE | `/documents/{id}` | — | 删除文档（不存在返回 404） |
| GET | `/documents` | — | 当前文档全集 |
| POST | `/corpus` | `{"docs":200,"seed":42}` | 清空并重建合成语料 |
| POST | `/query` | `{"query":"...","optimize":true}` | 布尔查询 |

查询成功返回 `200`：`{"docIds":[...],"count":n,"optimize":...,"trace":[交集顺序]}`；
解析失败返回 `400`：`{"error":"...","position":k,"query":"..."}`（`position` 为 0 基字符位置）。

## 请求样例（实际运行记录）

服务：`java -cp out/main boolsearch.Main serve --port 18099 --docs 100 --seed 42`

```bash
# 1. 布尔查询（优化开）：交集按倒排大小升序 [37,43,71]
curl -s -X POST localhost:18099/query -d '{"query":"apple AND banana AND NOT grape","optimize":true}'
# {"docIds":[6,57,61,64,68,75,78,89,94],"count":9,"optimize":true,
#  "trace":["AND intersect order sizes=[37, 43, 71] (optimized)"]}

# 2. 同一查询关闭优化：结果集完全一致
curl -s -X POST localhost:18099/query -d '{"query":"apple AND banana AND NOT grape","optimize":false}'
# {"docIds":[6,57,61,64,68,75,78,89,94],"count":9,...}

# 3. 交集顺序对比（df 差异明显）：优化后升序，朴素按书写顺序
curl -s -X POST localhost:18099/query -d '{"query":"mango AND apple AND kiwi","optimize":true}'
# trace: ["AND intersect order sizes=[31, 35, 37] (optimized)"]
curl -s -X POST localhost:18099/query -d '{"query":"mango AND apple AND kiwi","optimize":false}'
# trace: ["AND intersect order sizes=[35, 37, 31] (naive)"]

# 4. 纯 NOT（相对固定全集求补）
curl -s -X POST localhost:18099/query -d '{"query":"NOT kiwi"}'      # count=69

# 5. 未知词：本身为空集，NOT 未知词 = 全集
curl -s -X POST localhost:18099/query -d '{"query":"nosuchterm OR apple AND NOT nosuchterm"}'

# 6. 解析错误：HTTP 400，保留位置（EOF 位置 = 串长 9）
curl -s -X POST localhost:18099/query -d '{"query":"apple AND"}'
# {"error":"期望词项或 '('，但遇到 输入结束","position":9,"query":"apple AND"}

# 7. 新增文档 → 命中新文档；删除后 → 全集收缩、结果同步减少
curl -s -X POST localhost:18099/documents -d '{"id":1000,"text":"apple banana kiwi"}'
curl -s -X POST localhost:18099/query -d '{"query":"kiwi AND apple"}'   # 含 1000
curl -s -X DELETE localhost:18099/documents/1000
curl -s -X POST localhost:18099/query -d '{"query":"kiwi AND apple"}'   # 不再含 1000

# 8. 删除不存在文档：404
curl -s -X DELETE localhost:18099/documents/999
# {"error":"文档不存在: 999","position":-1}
```

完整脚本见 `examples/requests.sh`。

## 自动化测试（验收对照）

`./run-tests.sh` 共 24 项，关键覆盖：

- **穷举对照**：3 词词表 {a,b,c}，文档全集 = 全部 2³=8 个子集；枚举全部深度 ≤2 的
  查询共 **1227 条**，逐条与“逐文档布尔求值”的参考集合运算对照，**朴素计划、优化计划、
  参考语义三者结果一致**；另有 2000 条随机深度 ≤5 查询（含未知词 `z`）。
- **纯 NOT**：`NOT cherry` = 全集 − 倒排表；`NOT NOT x == x`；空全集下 `NOT` 为空。
- **未知词**：`zzz` → 空集；`NOT zzz` → 全集；与未知词交为空、求并不变。
- **删除文档**：倒排表与全集同步收缩，删除后 `NOT` 结果随之变小，重新加入后恢复；
  删除不存在文档返回 false / HTTP 404。
- **优化前后对比**：df 倾斜索引上验证优化计划按 `[3,10,100]` 升序求交、朴素计划按书写
  顺序 `[100,3,10]`；300 篇合成语料 × 500 随机查询优化前后结果一致。
- **错误位置**：9 组非法查询断言精确字符位置（空查询、缺操作数、括号未闭合、
  非法字符、缺运算符等）。
- **端到端**：真实启动 HTTP 服务，覆盖增删查、纯 NOT、未知词、错误位置、404。

## 实际运行记录

环境：OpenJDK 21.0.12.1，Linux，无 Maven（故采用纯 javac 构建）。

| 命令 | 结果 |
|---|---|
| `./build.sh` | 通过：`构建完成: out/main, out/test` |
| `./run-tests.sh`（首次） | **23/24 通过**；未通过项：`json: 嵌套结构往返`（NPE） |
| 修复后 `./run-tests.sh` | **24/24 通过** |
| `java -cp out/main boolsearch.Main demo` | 9 条演示查询正常，3 条非法查询正确报告位置 |
| `serve --port 18099` + 上述 curl 样例 | 全部符合预期（含 400/404 错误路径） |

**未通过项及处理（如实记录）**：首轮测试中 `json: 嵌套结构往返` 失败，原因是测试代码
用 `Map.of` 存放 `null` 值（`Map.of` 不允许 null，属测试自身问题，非被测代码缺陷）；
改为 `LinkedHashMap` 并将整数字面量改为 `Long`（与 JSON 解析器数字类型约定一致）后，
全部 24 项通过。另：`serve --port 18080` 首次启动因端口被占用失败（BindException），
换用 18099 端口正常。

## 设计说明

- **NOT 相对固定全集**：`NOT q` 定义为 `U \ eval(q)`，其中 `U` 为索引当前存活文档集合
  （删除文档即从 `U` 与各倒排表中移除）。因此 `NOT unknownterm` = 全集，空全集下任何
  `NOT` 均为空。
- **交集顺序优化**：AND 节点先求出全部子结果集，再按大小升序依次 `retainAll`；
  小集合先交可最快缩小中间结果，且为空即短路。优化只改变求值顺序，不改变结果集——
  这一点由穷举测试与随机对照测试共同保证。
- **错误位置**：词法错误（非法字符）在词法层抛出，语法错误在解析层抛出，均携带
  0 基字符偏移；HTTP 层原样透传为 `position` 字段，CLI 以 `^` 指示位置。
