# 运行记录（实际执行，如实记录）

- 日期：2026-09-24
- 环境：Ubuntu，OpenJDK 21.0.12（`javac`/`java`），无 Maven/Gradle；项目零第三方依赖。
- 所有命令均在仓库根目录执行；本文件与 `examples/output/` 中的产物由真实运行产生。

## 1. 构建

命令：

```bash
bash scripts/build.sh
```

结果：成功（退出码 0）。主源码编译到 `build/classes`，测试编译到 `build/test-classes`。

## 2. 自动化测试

命令：

```bash
bash scripts/run_tests.sh
# 等价于 build 后执行：
# java -cp build/classes:build/test-classes com.example.phrasesearch.tests.AllTests
```

结果（完整日志：`examples/output/test-run.log`）：

```
tests passed: 3152, failed: 0
ALL TESTS PASSED
```

| 测试组 | 通过断言数 | 失败 |
|---|---:|---:|
| Analyzer tests（切词/偏移/两种停用词位置语义） | 15 | 0 |
| Search semantics（精确短语、slop 穷举、重复词、跨字段、字段限定） | 506 | 0 |
| stop_gap search（停用词删除留洞、跨字段基址含洞） | 14 | 0 |
| Brute-force equivalence（穷举参考实现全枚举 + 固定种子随机差分） | 2563 | 0 |
| JSON tests（解析/序列化/转义/数字/错误） | 13 | 0 |
| HTTP server tests（真实起服务的端到端用例） | 41 | 0 |
| **合计** | **3152** | **0** |

进程正常退出（退出码 0，无挂起）。

### 开发过程中出现过、并已修复的失败（如实记录）

这些是在实现过程中真实遇到、定位并修复的缺陷，最终版本均已被测试覆盖：

1. **DFS 初始 `prevPos` 传成 0（应为 -1）**：导致每个词项的**位置 0 候选被整体漏掉**，
   title 中的精确短语全部不命中。通过临时插桩发现并修复。
2. **DFS 剪枝过度**：中间词项没有“为后续词项预留最小间隙（每个 +1）”，
   会把合法组合提前剪掉。改为按最终 `slopUsed` 下界剪枝。
3. **候选位置未按文档内排序**：倒排表按文档×字段插入，同文档内子序列未必按位置有序，
   DFS 的二分/剪枝依赖有序；改为文档级取出后显式排序。
4. **合成语料字段顺序**：初版用无序的 `Map.of` 再包成 `LinkedHashMap`，字段顺序不保证，
   导致跨字段拼接错位；改为显式按顺序构造（`fields(k1,v1,k2,v2,…)`）。
5. **JSON 解析两个 bug**：
   - 整数被错误解析成 `Double`。根因是 JLS 条件表达式数值提升——
     `cond ? Double.valueOf(s) : Long.valueOf(s)` 会把 `Long` 分支也提升为 `double`；
     改成显式 `if/else`（返回 `Number`）解决；
   - 指数标记 `e/E` 分支一度漏置 `isDouble`，已补回并加测试。
6. **测试框架数值比较**：JSON 反序列化的整数是 `Long`，Java 字面量是 `Integer`，
   `Objects.equals` 判为不等；`Assert` 改为按数值比较（List 元素同样递归处理）。
7. **HTTP 测试后 JVM 不退出**：自定义线程池未随 `server.stop()` 关闭，非守护工作线程
   使进程挂住；`PhraseHttpServer` 现在持有线程池并在 `stop()` 中 `shutdownNow()`。
8. 若干**测试预期笔误**（手工穷举位置算错、stop_gap 下用了本身就是停用词的 "a"、
   `count()` 的 long/int 比较、`List.of(null)` 不允许 null 等），均已更正为正确预期。

上述每一项都转化成了最终的自动化断言，例如：位置 0 不再漏（精确短语 title 命中）、
整数往返为 `Long`、服务关闭后 JVM 能退出（测试以退出码 0 结束）。

## 3. 服务真实运行

### 3.1 默认（standard：停用词保留占位）

```bash
java -cp build/classes com.example.phrasesearch.Main --port 8080
```

启动输出：

```
Phrase position search service started
  analyzer : standard
  docs     : 8
  port     : 8080
  endpoints: GET /health /config /docs ; GET|POST /search ; GET|POST /analyze
```

关键请求的真实结果（原始 JSON 保存在 `examples/output/01..11-*.json`）：

- `GET /health` → 200，`{"status":"ok","docs":8}`。
- `GET /search?q=quick brown fox&slop=0`
  → 1 篇文档命中，2 次出现：doc1 title `(0,1,2)`、body `(5,6,7)`，`slopUsed=0`。
- **重复词** `GET /search?q=that that&slop=0`
  → 仅 2 次相邻命中：doc3 `(0,1)`、`(5,6)`。
- **重复词 + slop** `q=that that&slop=5` → **8 次**，位置对
  `(0,1)(0,5)(0,6)(1,5)(1,6)(5,6)(5,11)(6,11)`，对应 slopUsed
  `0,4,5,3,4,0,5,4`；每条两个 `globalPosition` 严格递增、互不相同。
- **三连重复** `q=had had&slop=0` → `(0,1)(1,2)(7,8)` 共 3 次（没有位置复用）。
- **跨字段** `q=phrase search&slop=2` → 2 次：title 内 `(title:0,title:1)`，
  以及跨越边界的 `(title:0, body:3)`，`crossField=true, slopUsed=2`。
- **跨字段 + 停用词保留** `q=system the quick&slop=0`
  → 1 次精确跨字段命中 `(title:system@1, body:the@2, body:quick@3)`，
  `crossField=true`，证明默认模式停用词占位。
- **字段限定** `q=phrase search&slop=2&field=body` → 0 命中（phrase 只在 title）。
- `POST /search {"query":"alpha alpha","slop":4}` → doc6 中 5 对
  （穷举边界序列 alpha 位于 2,4,7,9：slop≤4 命中 5 对）。
- `POST /analyze {"text":"The quick, brown fox!"}`
  → 4? 实际 4 个词以外无停用词删除：`the/quick/brown/fox` 位置 0..3（停用词保留）。
- 错误请求实测：缺 `query` → **400**；`slop=-1` → **400**；
  未知 `field` → **400**；非法 JSON → **400**。

### 3.2 stop_gap（停用词删除、位置留洞）

```bash
java -cp build/classes com.example.phrasesearch.Main --port 8099 --analyzer stop_gap
```

- `GET /config` → `"analyzer":"stop_gap"`,
  `"stopwordMode":"removed_but_position_gap_preserved"`。
- doc5：title `index system`，body `the quick …`（the 被删、留位置洞）。
  - `q=system quick&slop=0` → **0 命中**（位置 1 与 3 之间隔着洞 2）；
  - `q=system quick&slop=1` → **1 次**跨字段命中
    `(title:1, body:3)`，`slopUsed=1, crossField=true`。
  - `POST /search {"query":"system the quick","slop":1}`
    → 查询中的停用词同样被删，`terms=["system","quick"]`，1 次命中。

> 备注：8081 端口在本机被其他进程占用（返回了非本服务的 405），
> stop_gap 实例改用 8099 端口验证。

## 4. 未通过项 / 已知限制

- **最终交付版本：无未通过测试（3152/3152 通过），无忽略的失败。**
- 已知功能边界（非缺陷，README “局限” 同步说明）：
  内存索引、单机；slop 仅“有序”语义，不支持词项换位；不做中文分词；
  不做相关性打分与摘要高亮。
