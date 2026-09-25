# streaming-aho-corasick

流式多模式匹配纯后端项目：Java 实现的本地文本检索库 + JSON HTTP 服务。
**不调用任何外部搜索服务或大模型**，全部输入来自内置的确定性合成语料或请求体。
零第三方依赖，仅需 JDK（开发于 OpenJDK 21，仅用 `com.sun.net.httpserver` 等 JDK API）。

## 能力

- **Aho–Corasick 多模式匹配**：Trie + failure link + dictionary link。
- **流式分块输入**：匹配器只保存“AC 当前状态 + 已消费计数”，模式可跨越任意块边界命中。
  - `feed(String)`：按 Unicode code point 消费（调用方须保证不把代理对拆开）。
  - `feedBytes(byte[])`：按 UTF-8 字节消费，**块可以切在一个多字节字符的内部**，
    解码器自动保留不完整尾部到下一块；流末残字节按畸形输入报错。
- **重叠匹配**：同一结束位置上，模式与其后缀模式同时命中时全部输出
  （如文本 `she`、模式 `she/he` 同时报出）。
- **重复模式独立身份**：相同字面的模式在 Trie 上共享路径，但各自的 `patternIndex`/`id`
  独立保留，都会产生命中。
- **空模式策略**（`emptyPatternPolicy`）：
  - `ERROR`：编译时拒绝任何空模式；
  - `SKIP`：忽略空模式（返回 `skippedIndices`）；
  - `MATCH_EVERY_POSITION`（默认）：空模式在每个 code point 边界产生一条零长度命中，
    长度 n 的文本共 n+1 个位置；任意分块方式与整体匹配结果完全一致。
- **Unicode 边界**：位置以 code point 偏移为准（`start/end`），同时报告 UTF-16 单元
  偏移（`charStart/charEnd`）；正确处理 BMP、补充平面（emoji 代理对 / UTF-8 四字节）、
  组合字符序列。
- **合成语料**：固定种子 LCG 生成的三个可复现 profile（`dna`、`sharedPrefix`、`unicode`），
  部分模式显式种植到文本中保证有命中。
- **规范输出顺序**：`(end, start, patternIndex)` 升序。分块按序拼接即全局有序，
  因此“分块结果拼接 == 整体匹配”可直接逐条比较。

## 目录结构

```
src/com/example/ac/                 核心库
  Pattern.java / Match.java         模式与命中记录（含双坐标）
  EmptyPatternPolicy.java           空模式策略
  Automaton.java                    AC 自动机（Trie/failure/dictionary link）
  Compiled.java / Engine.java       编译入口
  StreamingMatcher.java             流式匹配器（String + UTF-8 字节）
  NaiveMatcher.java                 朴素暴力匹配（仅测试 oracle）
  corpus/SyntheticCorpus.java       合成语料
  corpus/CorpusRunner.java          分块运行 + 跨块命中统计
  server/HttpJsonServer.java        JDK HTTP JSON 服务
  server/JsonParser.java/JsonWriter.java  手写 JSON
test/com/example/ac/test/           8 个测试类（迷你自研测试框架）
examples/                           请求样例（curl 脚本 + JSON）
build.sh / run-tests.sh / run-server.sh
RUNLOG.md                           实际构建/测试/运行记录
```

## 构建与测试

需要 JDK（建议 17+，开发环境为 21）。无需 Maven/Gradle：

```bash
./build.sh          # 编译到 build/classes 与 build/test-classes
./run-tests.sh      # 运行全部自动化测试（退出码 0/1）
./run-server.sh 8080 127.0.0.1   # 启动 JSON 服务（默认 127.0.0.1:8080）
```

## HTTP API

请求/响应均为 UTF-8 JSON。位置区间均为左闭右开。

| 方法/路径 | 说明 |
|---|---|
| `GET  /` | 服务信息与端点列表 |
| `GET  /api/health` | 健康检查 |
| `POST /api/match` | 一次性整体匹配 |
| `POST /api/sessions` | 创建流式会话（编译模式，返回 `sessionId`） |
| `POST /api/sessions/{id}/feed` | 喂入文本块（`chunk` 字符串或 `bytesBase64`），可多次调用 |
| `POST /api/sessions/{id}/finish` | 结束流，返回尾部命中（空模式最后一个边界） |
| `DELETE /api/sessions/{id}` | 释放会话 |
| `GET  /api/corpus?profile=&textLength=&patternCount=&includeText=` | 生成合成语料 |
| `POST /api/corpus/run` | 生成语料并分块跑匹配，返回逐块统计与跨块命中数 |

### `POST /api/match`

请求：

```json
{
  "text": "ushers",
  "emptyPatternPolicy": "MATCH_EVERY_POSITION",
  "patterns": [
    "he",
    {"id": "she-pattern", "literal": "she"},
    {"id": "empty", "literal": ""}
  ]
}
```

`patterns` 元素可以是字符串（id 自动为 `p0,p1,...`）或 `{"id","literal"}`。
响应命中字段：`patternId, start, end, charStart, charEnd, patternIndex, matched`。

### 流式会话

```bash
curl -s -X POST localhost:8080/api/sessions \
  -H 'Content-Type: application/json' \
  -d '{"patterns":["abcde"],"emptyPatternPolicy":"SKIP"}'
# -> {"sessionId":"sess-...", ...}

curl -s -X POST localhost:8080/api/sessions/sess-.../feed \
  -H 'Content-Type: application/json' -d '{"chunk":"abc"}'   # 模式尚未完成，0 命中
curl -s -X POST localhost:8080/api/sessions/sess-.../feed \
  -H 'Content-Type: application/json' -d '{"chunk":"de"}'    # abcde 跨块命中
curl -s -X POST localhost:8080/api/sessions/sess-.../finish -d '{}'
```

字节流（块可切在 emoji 的 UTF-8 四字节中间）：

```bash
curl -s -X POST localhost:8080/api/sessions/sess-.../feed \
  -H 'Content-Type: application/json' \
  -d '{"bytesBase64":"<base64 编码的任意 UTF-8 字节块>"}'
```

### `POST /api/corpus/run`

```json
{
  "profile": "dna",
  "textLength": 300,
  "patternCount": 20,
  "chunkSize": 7,
  "chunkUnit": "CODEPOINT",
  "emptyPatternPolicy": "MATCH_EVERY_POSITION",
  "includeMatches": false,
  "includeText": false
}
```

`chunkUnit` 为 `CODEPOINT` 或 `UTF8_BYTE`。响应 `run.chunks[]` 给出每块的
输入大小、产生命中数、`crossChunkCompleted`（起点落在本块起点之前、在本块内完成
的命中），并汇总 `totalCrossChunk`。`examples/` 下有可直接执行的脚本。

## 库用法

```java
List<Pattern> pats = List.of(Pattern.of("a", "he"), Pattern.of("b", "she"));
Compiled c = Engine.compile(pats, EmptyPatternPolicy.MATCH_EVERY_POSITION);

// 一次性
List<Match> all = Engine.match(c, "ushers");

// 流式（String 块）
StreamingMatcher m = new StreamingMatcher(c);
List<Match> sink = new ArrayList<>();
m.feed("ush", sink);
m.feed("ers", sink);
sink.addAll(m.finish());

// 流式（UTF-8 字节块，边界可切断多字节字符）
m = new StreamingMatcher(c);
m.feedBytes(chunk1, 0, chunk1.length, sink);
m.feedBytes(chunk2, 0, chunk2.length, sink);
sink.addAll(m.finish());
```

## 验收测试对应关系

| 验收点 | 覆盖测试 |
|---|---|
| 全部分块位置与朴素匹配比较 | `CrossChunkTest`（每个 code point/字节切点）、`FuzzTest`（400 轮随机文本/模式/切点，~1200 次全量逐条比较）、`CorpusTest`（三种合成语料 × 6 种块大小） |
| 空模式策略 | `EmptyPatternTest`、`CoreAhoCorasickTest`、HTTP 层 `ServerE2ETest` |
| Unicode 边界 | `UnicodeTest`（emoji 代理对、UTF-8 字节内部切分、组合字符） |
| 大量共享前缀 | `SharedPrefixTest`（600 个 a^k/a^k c 模式、800 字符 a-run，十万级重叠命中；2000 随机模式冒烟） |
| 重叠匹配 | `CoreAhoCorasickTest`（she/he、多级 dictionary link）、fuzz 全量 |
| 重复模式独立身份 | `CoreAhoCorasickTest`、`FuzzTest`（随机复制模式） |
| 跨块命中 | `CrossChunkTest`、`CorpusTest`（`totalCrossChunk > 0` 实测断言） |
| JSON 服务 | `ServerE2ETest`（真实 HTTP 服务器 + JDK HttpClient，含错误码） |

实际运行记录见 [RUNLOG.md](RUNLOG.md)。

## 设计说明与边界

- AC 的稀疏转移函数带缓存，与文本/模式字符集规模无关地建 trie，适合 Unicode 大字符集。
- 朴素 `NaiveMatcher` 是**独立实现**的暴力参考，AC 与流式层的正确性以它为 oracle 做差分测试。
- `feed(String)` 契约：不得把一个代理对拆在两个 chunk 里（Java String 按 UTF-16 索引时
  调用方应按 code point 切）；需要任意切分请使用 `feedBytes`，它按 UTF-8 自行拼装。
- 服务仅绑定回环地址、无持久化、无鉴权，定位为本地库的 JSON 外壳。
