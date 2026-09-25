# Unicode 规范化映射（unicode-normalization-mapping）

纯后端 Java 项目：本地文本规范化 + 原文偏移映射 + 内存检索 + JSON 服务。
不调用任何外部搜索服务或大模型；仅使用 JDK 21 标准库，零第三方依赖。

## 功能

- **文本规范化**：把任意输入文本转换为用于检索的规范化形式，同时记录每个
  规范化字符对应的**原文 UTF-16 偏移区间**。
- **本地检索**：对自建合成语料做规范化子串匹配，命中结果映射回**原文完整范围**
  （`start`/`end` 为原文 UTF-16 偏移，`matched` 为原文切片）。
- **JSON 服务**：基于 JDK 内置 `com.sun.net.httpserver` 的 HTTP 接口。

## 规范化形式与大小写策略（明确约定）

规范化流水线（`TextNormalizer`）：

1. **分段**：把输入切成若干"段"——一个起始码点（starter）加上其后的全部
   组合记号（Unicode 类别 Mn/Mc/Me）。孤立的组合记号自成一段。
2. **NFKD**：对每一段应用 `java.text.Normalizer` 的 **NFKD**
   （兼容分解 + 规范重排序）。因为整段一起规范化，规范等价的序列
   （如 `é` = U+00E9 与 `e` + U+0301）得到相同结果。输出保持**分解形式**。
   - 兼容分解同时处理：`ﬁ`(U+FB01) → `fi`、全角 `Ｙ` → `Y`、
     上标/圈字符等兼容字符。
3. **全大小写折叠（full case folding）**（`CaseFolder`）：
   - 先按 `Locale.ROOT` 小写化（处理 `İ`(U+0130) → `i` + U+0307 等多字符小写）；
   - 再应用 `String.toLowerCase` **不会**做的特殊折叠：
     `ß`(U+00DF) / `ẞ`(U+1E9E) → `ss`，`ſ`(U+017F) → `s`，
     `ς`(U+03C2) → `σ`，`ι`/`ͅ` → `ι`。
   - 即大小写策略 = **Unicode 全大小写折叠的近似**（CaseFolding 的 C+F 状态），
     不是简单小写化。这就是"大小写展开"：`STRASSE` 与 `Straße` 互相可检索。

**已知行为（有意为之）**：`İ`(U+0130) 按 Unicode CaseFolding 折为
`i` + U+0307，因此普通查询 `istanbul` **不能**命中 `İstanbul`
（测试 `testCaseFoldingDottedI` 固化该行为）。

## 偏移映射设计

- 对规范化文本的**每个 UTF-16 码元**记录其来源段的原文区间
  `[origStart, origEnd)`（`NormalizedText.origStart/origEnd` 数组）。
- 命中区间 `[ns, ne)` 映射回原文时取覆盖所有来源段的**最小闭包**：
  `start = min(origStart[ns..ne))`，`end = max(origEnd[ns..ne))`。
- **保证**（由测试 `testMappingNeverCutsCodePoints` 验证）：
  - 区间绝不切断码点（不会在代理对中间开始/结束）；
  - 区间绝不切断组合序列（段是映射的最小单位）；
  - 展开命中总是覆盖原文完整字符，例如查询 `ss` 命中 `ß` 时返回整个 `ß`，
    查询 `café`（组合形式）命中分解形式原文时返回完整的 `e` + U+0301。

## 目录结构

```
src/main/java/com/example/uninorm/
  TextNormalizer.java   规范化 + 偏移映射（NFKD + 大小写折叠）
  NormalizedText.java   规范化文本与映射数组、区间回映
  CaseFolder.java       全大小写折叠表
  SearchEngine.java     内存文档库 + 规范化子串检索
  Json.java             手写 JSON 解析/编码（零依赖）
  Server.java           HTTP JSON 服务（JDK 内置 HttpServer）
  Main.java             入口：server / normalize / search
src/test/java/com/example/uninorm/TestRunner.java   自动化测试（129 项断言）
data/corpus.txt         自建合成语料（5 篇文档）
examples/requests.sh    curl 请求样例
scripts/                build.sh / test.sh / server.sh
docs/verification.md    实际运行记录（命令与结果）
```

## 构建与运行

```bash
# 构建
bash scripts/build.sh

# 运行测试（129 项断言，含 HTTP 集成测试）
bash scripts/test.sh

# 启动服务（加载合成语料，端口 8080）
bash scripts/server.sh 8080
```

等价的手动命令：

```bash
mkdir -p build/main build/test
javac -encoding UTF-8 -d build/main $(find src/main/java -name '*.java')
javac -encoding UTF-8 -cp build/main -d build/test $(find src/test/java -name '*.java')
java -cp build/main:build/test com.example.uninorm.TestRunner
java -cp build/main com.example.uninorm.Main server 8080 data/corpus.txt
```

## API

| 方法 | 路径 | 请求体 | 说明 |
|---|---|---|---|
| GET | `/health` | — | 健康检查 |
| GET | `/documents` | — | 列出文档 ID |
| POST | `/documents` | `{"id","text"}` | 索引/替换文档 |
| POST | `/normalize` | `{"text"}` | 返回规范化形式与段级映射 |
| POST | `/search` | `{"query","max"?}` | 检索，命中映射回原文区间 |

### 请求样例（实际运行输出，详见 docs/verification.md）

```bash
# ß 的大小写展开：STRASSE 命中 Straße
curl -s -X POST http://127.0.0.1:8080/search \
  -H 'Content-Type: application/json' --data '{"query":"STRASSE"}'
```
```json
{"query":"STRASSE","normalizedQuery":"strasse","hitCount":3,"hits":[
 {"docId":"doc:german","start":4,"end":10,"matched":"Straße"},
 {"docId":"doc:german","start":22,"end":29,"matched":"STRASSE"},
 {"docId":"doc:german","start":34,"end":41,"matched":"strasse"}]}
```

```bash
# 组合形式查询命中分解形式原文，区间覆盖完整组合序列
curl -s -X POST http://127.0.0.1:8080/search \
  -H 'Content-Type: application/json' --data '{"query":"café"}'
```
```json
{"hitCount":3,"hits":[
 {"docId":"doc:cafe","start":3,"end":7,"matched":"café"},
 {"docId":"doc:cafe","start":21,"end":25,"matched":"CAFÉ"},
 {"docId":"doc:cafe","start":37,"end":42,"matched":"café"}]}
```
（第三个命中 `matched` 为分解形式 `cafe` + U+0301，区间 [37,42) 覆盖全部 5 个 UTF-16 码元。）

更多样例见 `examples/requests.sh`。

## 验收覆盖对照

| 验收项 | 实现/测试 |
|---|---|
| 组合字符 | NFKD 段级规范化；`testCombiningCharacters`、`testComposedVsDecomposedSearch` |
| 大小写展开 | ß→ss 等全折叠；`testCaseExpansionSharpS`（含 `ss` 命中整个 `ß`） |
| 多字节字符 | emoji 代理对、CJK；`testMultiByteCharacters` |
| 映射区间不切断码点 | 段级映射 + 不变量测试 `testMappingNeverCutsCodePoints`（代理对/组合序列边界检查） |
| 命中映射回原文完整范围 | `SearchEngine.search` 返回原文 `[start,end)` 与 `matched` 切片 |

## 已知限制

- 检索为规范化**子串匹配**（允许重叠命中），非词项倒排索引；语料规模定位为小规模。
- 大小写折叠表是 Unicode CaseFolding 的常用子集（覆盖拉丁/希腊常见特例），
  未内置全部罕见折叠条目。
- 段级映射意味着段内更细粒度的对应关系不保留（例如 `ﬁ` 的 `f` 与 `i`
  都映射到整个 `ﬁ`），这是"不切断码点/组合序列"保证的直接代价。
