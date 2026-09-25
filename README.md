# 流式多模式匹配（Streaming Multi-Pattern Matching）

零依赖纯 Java 实现的**流式 Aho-Corasick 多模式匹配库**与 **JSON HTTP 服务**。
不调用任何外部搜索服务或大模型；所有输入文本均由内置的、确定性种子可复现的
**合成语料生成器**产出（或由请求直接内联给出）。无前端。

- 语言/运行时：Java 21（仅用 JDK 标准库，HTTP 用 `com.sun.net.httpserver`，JSON 手写）
- 构建：`javac` 直接编译，无 Maven/Gradle、无第三方依赖
- 位置单位：**Unicode 码点（code point）**，位置相对于整个逻辑文本流，与分块无关

---

## 1. 功能与设计要点

| 需求 | 实现 |
| --- | --- |
| Aho-Corasick 多模式匹配 | `AhoCorasick`：trie + BFS 失败链 + 输出链聚合，goto 转移按需记忆化 |
| 流式 / 跨块命中 | `StreamMatcher`：增量喂入码点 / char / UTF-8 字节块，自动拼接跨块状态；命中位置为全局码点偏移 |
| 重叠匹配 | 失败链聚合输出，同一位置的多个模式（he/she/hers、aa/aaa…）全部产出 |
| 重复模式独立身份 | 模式按字符串去重存入 trie，但每个重复模式在输出列表中保留**各自独立的模式 ID**（0 起插入编号；服务还支持显式外部 ID） |
| 空模式策略 | `SKIP`（默认）/ `BEFORE` / `AFTER`；空流、分块下均严格满足 n+1 个位置 |
| Unicode 边界 | 码点语义；跨块 UTF-8 不完整多字节序列自动留续；char 块可切在代理对中间；覆盖 emoji、区域指示符、ZWJ 序列、组合字符、孤立代理 |
| 大量共享前缀 | 2000+ 共享长前缀模式的合成语料，trie 深而窄，验证构造与输出链 |
| 验收比较 | `NaiveMatcher`：O(n·m) 朴素枚举作为独立 oracle；所有路径（一次性 AC、码点/char/字节分块流式）与朴素逐条比较；另有 4000 组随机差分测试 |

### 1.1 空模式策略

长度 n（码点）的流上，每个空模式在间隙位置 p ∈ [0, n] 共 **n+1** 个命中
（`Match(id, p, p, "")`）。三种策略决定产出时机（最终集合相同）：

- `SKIP`：忽略空模式。
- `BEFORE`：消费第 i 个码点**之前**产出位置 i；`finish()` 时产出位置 n。
- `AFTER`：流开始时产出位置 0；消费第 i 个码点**之后**产出位置 i+1；空流在 `finish()` 产出位置 0。

无论逐码点喂入还是按任意块喂入，最终空命中集合恒为完整的 n+1 个；差别仅在命中
随哪个 `drainMatches()` 批次可见（归属于交付该码点的数据块）。

### 1.2 目录结构

```
src/com/example/streammatch/
  Match.java                 命中记录（patternId/start/end/pattern，码点位置）
  EmptyPatternPolicy.java    SKIP / BEFORE / AFTER
  AhoCorasick.java           AC 自动机（无状态、可共享）
  StreamMatcher.java         流式状态机（码点/char/UTF-8 字节增量喂入）
  NaiveMatcher.java          朴素参照实现（oracle）
  CorpusGenerator.java       四类自建合成语料
  Long2IntOpenHashMap.java   goto 转移记忆化用哈希表
  Main.java                  CLI：demo 自检 / server
  json/Json.java             零依赖 JSON 解析与序列化
  server/JsonHttpService.java JSON HTTP 服务 + 与传输解耦的业务方法
  tests/                     自包含迷你测试框架 + 9 个测试类 + TestAll 入口
examples/                    请求样例
examples/responses/          上述样例对真实服务的实际响应（已保存）
build.sh / run-tests.sh      编译 / 测试脚本
```

---

## 2. 构建与运行

需要 JDK 17+（开发与验证环境为 OpenJDK 21）。

```bash
# 编译（输出到 build/classes）
./build.sh

# 命令行演示 + 自检（一次性/分块/字节分块全部与朴素匹配核对）
java -Dfile.encoding=UTF-8 -cp build/classes com.example.streammatch.Main demo

# 启动 JSON 服务（默认 8080，可传端口）
java -Dfile.encoding=UTF-8 -cp build/classes com.example.streammatch.Main server 8080

# 全部自动化测试（失败时退出码非零）
./run-tests.sh
```

---

## 3. HTTP/JSON 服务

| 方法与路径 | 说明 |
| --- | --- |
| `GET  /health` | 健康检查 |
| `GET  /corpora` | 列出内置合成语料 |
| `POST /corpus?name=&seed=` | 生成一份合成语料（body 可 `{"includeText":false}`） |
| `POST /match` | 一次性匹配；默认自动与朴素匹配核对 |
| `POST /match/stream` | 服务端切块（码点或 UTF-8 字节）后逐块流式匹配，逐块返回全局位置命中并核对 |

### 3.1 请求字段

`/match`：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `text` | string | 内联文本（与 `corpusName` 二选一） |
| `corpusName` | string | `sharedPrefix` / `overlap` / `unicode` / `randomDna` |
| `seed` | int | 语料随机种子（默认 42） |
| `patterns` | (string\|`{id,pattern}`)[] | 模式数组；字符串元素按 0 起下标编号；对象元素可给显式 `id`（重复模式可同 ID 也各自输出一条） |
| `emptyPolicy` | string | `SKIP` / `BEFORE` / `AFTER`（默认 SKIP） |
| `compareWithNaive` | bool | 默认 true，返回 `matchesNaive` 核对结果 |

`/match/stream` 额外字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `chunkCodePoints` | int | 按码点切块大小（默认 7） |
| `byteMode` | bool | true 时改按 UTF-8 **字节**切块（会切断多字节字符），验证跨块 |
| `chunkBytes` | int | 字节块大小（默认 3） |

响应中的命中：`patternId`、`start`（含）、`end`（不含）、`length`、`pattern`；
位置均为码点单位。流式响应还含每个块的 `span` 与该块 `hits`，以及
`matchesStream`（流式 == 一次性 AC）与 `matchesNaive`（流式 == 朴素）两个核对布尔值。

### 3.2 请求样例（curl）

```bash
# 健康检查
curl -s http://localhost:8080/health

# 经典重叠 + 重复 "he"（ID 0 与 4 独立产出）
curl -s -X POST http://localhost:8080/match \
  -H 'Content-Type: application/json; charset=utf-8' \
  --data-binary @examples/match-overlap.json

# 显式外部 ID（两个不同 ID 的 "she" 各自输出）
curl -s -X POST http://localhost:8080/match \
  -H 'Content-Type: application/json' \
  --data-binary @examples/match-explicit-ids.json

# 按码点切块（chunkCodePoints=2，含 emoji/CJK）
curl -s -X POST http://localhost:8080/match/stream \
  -H 'Content-Type: application/json; charset=utf-8' \
  --data-binary @examples/stream-codepoints.json

# 按 1 个 UTF-8 字节切块（刻意切断每个多字节字符）
curl -s -X POST http://localhost:8080/match/stream \
  -H 'Content-Type: application/json; charset=utf-8' \
  --data-binary @examples/stream-bytes.json

# 空模式 BEFORE 策略 + 两个重复空模式
curl -s -X POST http://localhost:8080/match/stream \
  -H 'Content-Type: application/json' \
  --data-binary @examples/stream-empty-patterns.json

# 合成语料
curl -s -X POST 'http://localhost:8080/corpus?name=overlap&seed=3' -d '{"includeText": false}'
curl -s -X POST http://localhost:8080/match \
  -H 'Content-Type: application/json' \
  --data-binary @examples/match-corpus.json
```

样例请求文件见 `examples/`，对真实运行服务的响应存档见 `examples/responses/`
（内容较大的语料匹配响应只保存了前 3 条命中）。错误请求返回 HTTP 400 与
`{"ok":false,"error":...}`；方法不允许返回 405。

### 3.3 库 API（脱离 HTTP 使用）

```java
AhoCorasick ac = new AhoCorasick(List.of("he", "she", "hers", "he"),
                                 EmptyPatternPolicy.SKIP);

// 一次性
List<Match> all = ac.matchAll("ushers");

// 流式：码点 / char / UTF-8 字节，任意分块
StreamMatcher sm = new StreamMatcher(ac);
sm.feedChunk("us");                 // 块风格（命中按块批次可见）
sm.feedBytes(utf8Bytes, 0, 3, true); // 或原子风格 feedCodePoint/feedChars/feedBytes
List<Match> tail = sm.finish();
```

---

## 4. 合成语料

全部由 `CorpusGenerator` 以确定性 `Random(seed)` 生成，同种子可复现，不读外部数据：

- `sharedPrefix`：2004 个模式共享长前缀 `pr3fix/0000/`，文本注入 500 个确定命中 + 噪声。
- `overlap`：he/she/his/hers（含重复 `he`）、周期模式 aa/aaa/aaaa、ahahaha 等，密集重叠。
- `unicode`：CJK、emoji（😀）、区域指示符序列（🇯🇵）、ZWJ 家庭（👨‍👩‍👧）、
  组合字符（e + U+0301）、拆开的代理与 ZWJ 碎片。
- `randomDna`：A/C/G/T 小字母表，2000 个 6–14 码点模式，20 万码点文本，高自然命中率。

---

## 5. 自动化测试

`src/com/example/streammatch/tests/`（零依赖迷你断言框架，`TestAll` 统一入口，共 10 个测试类）：

1. `AhoCorasickTest` —— 经典重叠、重复模式独立 ID、重叠计数、边界。
2. `StreamMatcherTest` —— 1..9 的 char 分块、跨块模式拼接、drain 增量与幂等 finish、
   UTF-8 字节块、流尾残留/畸形输入报错。
3. `UnicodeTest` —— 8 种 Unicode 文本 × 码点块 / char 块（切代理对）/ 1..13 字节块，
   并断言位置是码点偏移而非 char 偏移。
4. `EmptyPatternTest` —— 三种策略、空流、重复空模式、逐批产出时机、n+1 集合。
5. `NaiveEquivalenceTest` —— **4000 组随机差分测试**：随机文本 × 模式表（含空串、
   从文本截取的子串、随机复制重复模式）× 三种喂入方式 × 随机块大小，全部位置与朴素逐条相等。
6. `SharedPrefixTest` —— 2000+ 共享前缀模式全量一致、逐模式身份抽检、流式小块一致。
7. `CorpusTest` —— 确定性复现、规模、未知语料报错。
8. `JsonTest` —— JSON 往返、Unicode 转义/代理对、数字、保序、畸形输入拒绝。
9. `ServiceTest` —— 服务业务方法 + 真实 HTTP 回环（随机端口）：全部端点、400/405、
   显式 ID、码点与字节两种流式切块、空策略计数。
10. `PerfTest` —— 2000 模式 × 20 万码点规模；全量 AC 与流式一致且足够快，
    2 万码点前缀上与朴素三方核对并断言 AC 不慢于朴素。

**实际运行结果（OpenJDK 21，本机）：**

```
测试结果: 481 通过, 0 失败, 耗时 3101 ms
```

性能概览（同一次运行；朴素 O(n·m) 只在 2 万码点前缀上做对照）：

```
sharedPrefix  模式=2004  文本=109500码点  构造=1ms  AC全量=2ms   流式=4ms   命中=8033
              | 前缀20000码点: AC=1ms  朴素=306ms  前缀命中=3036
randomDna     模式=2000  文本=200000码点  构造=3ms  AC全量=9ms   流式=12ms  命中=15080
              | 前缀20000码点: AC=0ms  朴素=423ms  前缀命中=1673
```

> 朴素参照故意保持最简单的 O(n·m) 枚举，不做任何算法优化，以保证它与 AC 是
> **两份独立实现**；因此全量 2000 模式 × 20 万码点规模上的朴素对照只取前缀执行。

`Main demo` 的 23 项自检（4 类语料 × 码点块/字节块、三种空策略、Unicode 等）在本次
验证中亦全部 `PASS`。**无未通过项。**

---

## 6. 关键语义备忘（容易踩坑的点）

- `Match.start/end` 是**码点**偏移：`"a😀b"` 中 `b` 的位置是 2（按 char 则是 3）。
- 经典样例 `ushers` 对 `[he, she, his, hers, he]` 的输出：
  `she@[1,4)`、`he(id=0)@[2,4)`、`he(id=4)@[2,4)`、`hers@[2,6)` ——
  两个重复 `he` 是两条独立记录。
- UTF-8 字节流在 strict（默认）模式下遇到流尾仍不完整的序列会在 `finish()` 抛
  `IllegalStateException`；畸形前导/孤立连续字节立即抛 `IllegalArgumentException`。
- 传 `strict=false` 时畸形字节按 JDK 解码器替换为 U+FFFD。
