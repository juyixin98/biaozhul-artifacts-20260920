# unicode-norm-map — Unicode 规范化映射检索库与 JSON 服务

纯后端 Java 项目：对文本做 Unicode 规范化并建立**原文偏移映射**，在规范化后的
文本上检索，把命中区间精确映射回**原文完整范围**。零第三方依赖（只用 JDK），
不调用任何外部搜索服务或大模型，语料为自建合成语料。

## 规范化策略（明确声明）

管线：**逐码点 NFKD → 大小写折叠**，文本与查询走完全相同的管线。

1. **规范化形式：NFKD（兼容分解）**，对原文**每个码点独立**应用。
   - 选 NFKD 而非 NFC/NFKC 的原因：合成（NFC/NFKC）会把多个原文码点合并成一个，
     映射不再可逆；分解只会把**一个**原文码点展开成一个序列，且展开结果不会与
     相邻码点合并，因此规范化后的每个字符都能唯一归属到某个原文码点。
   - 附带检索收益：兼容字符被折叠 —— 连字 `ﬁ→fi`、全角 `Ａ→A`、
     罗马数字 `Ⅳ→iv`、`™→tm`、圈数字 `①→1`、数学粗体 `𝐀→a`、
     平方日文 `㌀→アパート` 等。
2. **大小写策略：与语言环境无关的折叠**（`Locale.ROOT`），实现为对 NFKD 结果
   逐码点做 `toUpperCase(ROOT)` 再 `toLowerCase(ROOT)`。先大写是为了捕获
   一对多大写展开，典型如 `ß→SS→ss`，使 `Straße`/`STRASSE`/`strasse` 互相命中。

### 已知取舍（如实声明）

- **跨码点不重排**：逐码点处理刻意跳过了 Unicode 规范化的"跨码点规范重排"
  （相邻组合符按 canonical combining class 排序）这一步。由于文本与查询使用
  同一管线，匹配结果不受影响；输出是确定、幂等（对本管线）的检索键，
  不保证是规范意义上的 Unicode 规范化串。
- **不应用语言相关规则**：土耳其语 `İ/i`（`İ` 折叠为 `i`+组合点 U+0307，
  查询 `istanbul` 不会命中 `İSTANBUL`，查询 `i`+U+0307 会命中）、
  立陶宛语、希腊语词尾 sigma（`ς` 与 `σ` 不互命中）等均按 Unicode 默认
  简单映射处理。

## 偏移映射

`TextNormalizer.normalize(s)` 返回 `NormalizedText`，包含：

- `normalized()`：规范化检索键；
- 反向映射表：规范化串每个 UTF-16 单元 → 来源原文码点序号；
- 原文码点 → 原文 UTF-16 / UTF-8 起始偏移表。

`mapRange(normStart, normEnd)` 把规范化串上的半开区间映射回原文区间
`OffsetRange`，同时给出三种坐标：**UTF-16**（Java `String` 下标）、
**码点序号**、**UTF-8 字节偏移**。区间边界按码点表直接取得，
**从构造上保证不会切断码点**（UTF-16 边界不会落在代理对中间，UTF-8 边界
不会落在多字节序列中间）；若调用方传入落在规范化串代理对中间的区间，
直接抛 `IllegalArgumentException` 拒绝。

展开示例：`Straße` 规范化为 `strasse`（7 字符），命中 `ss` 映射回原文
第 5 个码点 `ß`（UTF-16 `[4,5)`，UTF-8 `[4,6)`）。

## 检索

`SearchEngine` 在规范化键上做 `String.indexOf` 子串匹配（枚举重叠命中），
每个命中经 `mapRange` 映射回原文完整范围；映射到同一原文区间的命中去重。
查询为空返回空结果，`limit<=0` 表示不限。

## 构建 / 测试 / 运行

要求 JDK 17+（开发验证用 OpenJDK 21.0.12）。无需 Maven/Gradle，无网络依赖。

```bash
./build.sh        # 编译库与测试到 build/
./test.sh         # 编译并运行全部自动化测试（27 个）
./run-server.sh   # 启动 JSON 服务，默认 127.0.0.1:8080，可传端口参数
```

## JSON API

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 存活探针 |
| GET | `/search?q=...&limit=N` | URL 参数查询 |
| POST | `/search` | body `{"query":"...","limit":N}` |
| GET | `/normalize?text=...` | 查看规范化键 |
| GET | `/docs` | 列出语料文档及其规范化键 |

命中对象字段：`docId`、`startUtf16/endUtf16`、`startCodePoint/endCodePoint`、
`startUtf8/endUtf8`、`matchedOriginal`（原文切片，半开区间 `[start,end)`）。

请求样例与真实响应见 [`samples/`](samples/README.md)。

## 目录结构

```
src/com/example/uninorm/     库与服务源码
  TextNormalizer.java        规范化 + 映射表构建
  NormalizedText.java        规范化结果与 mapRange
  OffsetRange.java           三坐标原文区间
  SearchEngine.java          规范化子串检索
  SearchServer.java          JDK 内置 HttpServer 的 JSON 服务
  Json.java                  极简 JSON 读写（自足）
  Corpus.java                自建合成语料（9 篇）
  Main.java                  服务入口
tests/com/example/uninorm/   自建断言的自动化测试（无 JUnit 依赖）
samples/                     请求样例与真实响应
```

## 验收覆盖说明

- **组合字符**：`é`(U+00E9) 与 `e`+U+0301 互命中；组合符自身是独立原文码点时
  单独命中也映射正确（`OffsetMappingTest`、`SearchEngineTest.accentedCafe`）。
- **大小写展开**：`ß→ss` 一对多展开，`Straße`/`STRASSE`/`strasse` 互命中，
  且命中映射回原文 6 个码点而非规范化的 7 个字符（`SearchEngineTest.sharpS`）。
- **多字节字符**：全角、CJK、数学粗体字母（代理对）、emoji（含肤色修饰符
  双码点序列）均覆盖；UTF-8 偏移与实际编码逐字节核对
  （`OffsetMappingTest.mixedWidthUtf8Offsets`、`SearchEngineTest.fullWidthAndBold`）。
- **不切断码点**：`FuzzBoundaryTest` 用固定种子从"坑点字母表"（组合符、ß、
  连字、全角、代理对、emoji 等 22 种码点）随机生成 3000 个串，对规范化键的
  **每一个码点对齐子区间**（共 10 万+ 个）验证映射区间的 UTF-16/UTF-8 边界
  均落在码点边界上；另验证映射单调性与管线幂等性。
- **HTTP 端到端**：`HttpServiceTest` 在临时端口启动真实服务，用 JDK HttpClient
  验证 GET/POST、limit、错误码（400/405）。

## 实际运行记录（如实）

环境：OpenJDK 21.0.12，Linux 6.8.0-90-generic，2026-09-24。

```text
$ ./test.sh
== compiling library ==
== compiling tests ==
== build OK ==
== running tests ==
PASS  NormalizationTest.composedEqualsDecomposed
PASS  NormalizationTest.sharpSExpands
PASS  NormalizationTest.compatibilityCharacters
PASS  NormalizationTest.dottedCapitalI
PASS  NormalizationTest.greekCaseFolding
PASS  NormalizationTest.mathematicalBold
PASS  NormalizationTest.emojiPassThrough
PASS  NormalizationTest.idempotentOnCorpus
PASS  NormalizationTest.edgeCases
PASS  OffsetMappingTest.precomposedExpansionPointsBack
PASS  OffsetMappingTest.decomposedInputMarkOwnsOffsets
PASS  OffsetMappingTest.caseExpansionPointsBack
PASS  OffsetMappingTest.surrogatePairCannotBeCut
PASS  OffsetMappingTest.supplementaryFoldsToAscii
PASS  OffsetMappingTest.mixedWidthUtf8Offsets
PASS  OffsetMappingTest.invalidRanges
PASS  SearchEngineTest.cyrillic
PASS  SearchEngineTest.greek
PASS  SearchEngineTest.symbols
PASS  SearchEngineTest.sharpS
PASS  SearchEngineTest.accentedCafe
PASS  SearchEngineTest.ligatureExpansion
PASS  SearchEngineTest.fullWidthAndBold
PASS  SearchEngineTest.limitAndEdgeCases
PASS  FuzzBoundaryTest.allRangesRespectCodePointBoundaries
PASS  FuzzBoundaryTest.mappingIsMonotonic
PASS  HttpServiceTest.endToEndHttp
---------------------------------------------
tests run: 27, passed: 27, failed: 0 (2326 ms)
```

服务实测（8080/18080 被本机其他进程占用，改用临时端口 41637）：

```text
$ java -cp build/classes com.example.uninorm.Main 0
unicode-norm-map service listening on http://127.0.0.1:41637
loaded 9 synthetic documents
```

随后用 curl 执行的请求与响应原样保存在 `samples/*.json`。

### 开发中发现并已修复的问题（首次运行未通过项）

首轮 `./test.sh` 为 24 通过 / 3 失败，均为**测试期望值错误**，实现行为正确：

1. `NormalizationTest.compatibilityCharacters`：期望 `㌀`(U+3300) 展开为
   预合成假名 `アパート`，实际 NFKD 输出为分解形式 `アパート`
   （`パ` 被进一步分解为 `ハ`+U+309A 组合半浊点）。已按 NFKD 真实行为修正期望。
2. `SearchEngineTest.sharpS`：期望 3 个命中，实际语料 `doc-german` 中
   `Straße` 出现两次，共 4 个命中。已修正期望。
3. `HttpServiceTest.endToEndHttp`：同源问题，POST 响应 `count` 为 4。已修正。

修正后全部 27 个测试通过，当前无未通过项。
