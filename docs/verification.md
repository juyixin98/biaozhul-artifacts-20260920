# 实际运行记录

环境：OpenJDK 21.0.12.1，Linux 6.8.0-90-generic，无 Maven/Gradle，零第三方依赖。

## 1. 构建与测试

命令：

```bash
mkdir -p build/main build/test
javac -encoding UTF-8 -d build/main $(find src/main/java -name '*.java')
javac -encoding UTF-8 -cp build/main -d build/test $(find src/test/java -name '*.java')
java -cp build/main:build/test com.example.uninorm.TestRunner
```

最终结果：

```
PASSED: 129, FAILED: 0
```

### 开发过程中实际出现并已修复的问题（如实记录）

1. **编译错误 ×2**：`Json.java` 与 `TestRunner.java` 的 javadoc 注释里写了字面
   `\uXXXX`，Java 编译器在词法分析前处理 unicode 转义（注释中也不例外），报
   `illegal unicode escape`。修复：注释改为文字描述。
2. **测试失败 ×1（首轮 112 通过 / 1 失败）**：
   `normalized é is decomposed 'e' + U+0301` 失败。原因：最初对每段应用的是
   **NFKC**，而 NFKC 会做**正则组合**，`é`(U+00E9) 经 NFKC 仍是组合形式，
   导致组合/分解两种原文不能互相命中。修复：改用 **NFKD**（只分解不组合），
   输出保持分解形式。修复后 129 项全部通过。
3. **测试语料字面量不确定性**：编辑器/传输层会把源码中的 `é` 规范化为同一种
   形式，导致"组合 vs 分解"两个测试常量实际相同。修复：测试文件全部非 ASCII
   字面量改写为 `\uXXXX` 转义（纯 ASCII 源文件），形态确定。

## 2. 服务运行与请求样例（实际输出）

启动：

```bash
java -cp build/main com.example.uninorm.Main server 8080 data/corpus.txt
# 输出：listening on http://127.0.0.1:8080 (5 documents)
```

以下为本机实际 curl 输出（2026-09-24）：

```
=== GET /health ===
{"status":"ok"}

=== GET /documents ===
{"ids":["doc:cafe","doc:german","doc:ligature","doc:emoji","doc:turkish"]}

=== POST /normalize {"text":"Straße"} ===
{"original":"Straße","normalized":"strasse","segments":[
 {"normStart":0,"normEnd":1,"norm":"s","origStart":0,"origEnd":1,"orig":"S"},
 {"normStart":1,"normEnd":2,"norm":"t","origStart":1,"origEnd":2,"orig":"t"},
 {"normStart":2,"normEnd":3,"norm":"r","origStart":2,"origEnd":3,"orig":"r"},
 {"normStart":3,"normEnd":4,"norm":"a","origStart":3,"origEnd":4,"orig":"a"},
 {"normStart":4,"normEnd":6,"norm":"ss","origStart":4,"origEnd":5,"orig":"ß"},
 {"normStart":6,"normEnd":7,"norm":"e","origStart":5,"origEnd":6,"orig":"e"}]}

=== POST /normalize {"text":"café"}（组合形式 é=U+00E9）===
{"original":"café","normalized":"café","segments":[
 {"normStart":0,"normEnd":1,"norm":"c","origStart":0,"origEnd":1,"orig":"c"},
 {"normStart":1,"normEnd":2,"norm":"a","origStart":1,"origEnd":2,"orig":"a"},
 {"normStart":2,"normEnd":3,"norm":"f","origStart":2,"origEnd":3,"orig":"f"},
 {"normStart":3,"normEnd":5,"norm":"é","origStart":3,"origEnd":4,"orig":"é"}]}
（注：响应中 normalized 的 "é" 实际为分解形式 e + U+0301，normStart 3..5 两个码元。）

=== POST /search {"query":"STRASSE"} ===
{"query":"STRASSE","normalizedQuery":"strasse","hitCount":3,"hits":[
 {"docId":"doc:german","start":4,"end":10,"matched":"Straße"},
 {"docId":"doc:german","start":22,"end":29,"matched":"STRASSE"},
 {"docId":"doc:german","start":34,"end":41,"matched":"strasse"}]}

=== POST /search {"query":"café"}（组合形式查询）===
{"hitCount":3,"hits":[
 {"docId":"doc:cafe","start":3,"end":7,"matched":"café"},
 {"docId":"doc:cafe","start":21,"end":25,"matched":"CAFÉ"},
 {"docId":"doc:cafe","start":37,"end":42,"matched":"café"}]}
（第三个命中为分解形式原文，区间 [37,42) 完整覆盖 cafe + U+0301 共 5 个码元。）

=== POST /search {"query":"ss"} ===
{"hitCount":5,"hits":[
 {"docId":"doc:german","start":8,"end":9,"matched":"ß"},
 {"docId":"doc:german","start":26,"end":28,"matched":"SS"},
 {"docId":"doc:german","start":38,"end":40,"matched":"ss"},
 {"docId":"doc:german","start":49,"end":50,"matched":"ß"},
 {"docId":"doc:german","start":63,"end":64,"matched":"ß"}]}
（查询 ss 命中 ß 时区间扩展为完整的 ß，不会只覆盖一半。）

=== POST /search {"query":"fine"} ===
{"hitCount":2,"hits":[
 {"docId":"doc:ligature","start":11,"end":14,"matched":"ﬁne"},
 {"docId":"doc:ligature","start":35,"end":39,"matched":"Fine"}]}

=== POST /search {"query":"😀"} ===
{"hitCount":1,"hits":[{"docId":"doc:emoji","start":11,"end":13,"matched":"😀"}]}
（区间 [11,13) 覆盖完整代理对。）

=== POST /search {"query":"日本語"} ===
{"hitCount":1,"hits":[{"docId":"doc:emoji","start":41,"end":44,"matched":"日本語"}]}

=== POST /documents {"id":"doc:new","text":"Grüß Gott, die STRASSE ist schön."} ===
{"indexed":"doc:new","documents":6}
随后 {"query":"strasse"} 命中 4 条，含 {"docId":"doc:new","start":15,"end":22,"matched":"STRASSE"}。

=== 错误请求 {"query":123} ===
{"error":"missing or non-string field: query"}
```

## 3. 未通过项

最终交付状态：**无未通过项**（129/129 通过）。
开发过程中的失败项已全部修复，见上文第 1 节。
