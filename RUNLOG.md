# 运行记录（RUNLOG）

记录实际执行的命令、结果、首轮失败与修复。时间：2026-09-24，主机 Linux 6.8（Ubuntu 24.04）。

## 环境

```
$ java -version
openjdk version "21.0.12.1" 2026-08-18
OpenJDK 64-Bit Server VM (build 21.0.12.1+1-1-24.04.4-Ubuntu)
$ javac -version
javac 21.0.12.1
```

环境中没有 Maven/Gradle，因此项目零外部依赖：HTTP 用 JDK 内置
`com.sun.net.httpserver`，JSON 手写，测试用自研迷你框架，全部以 `javac` 构建。

## 构建

```
$ ./build.sh
[build] compiling main sources...
[build] compiling test sources...
[build] OK -> build/classes, build/test-classes
```

退出码 0，无警告。

## 自动化测试

```
$ ./run-tests.sh
PASS core-aho-corasick (32 checks)
PASS empty-pattern-policy (27 checks)
PASS unicode-boundaries (43 checks)
PASS cross-chunk-hits (104 checks)
PASS fuzz-vs-naive (1200 checks)
PASS large-shared-prefix (165756 checks)
PASS synthetic-corpus (65 checks)
PASS server-e2e (41 checks)
----
8 test cases, 167268 assertions, 8 passed cases, 0 failures
RESULT: ALL TESTS PASSED

real    0m4.011s
```

退出码 0。说明：`large-shared-prefix` 的 16 万+ assertion 是把 600 个 a^k/a^k c
共享前缀模式在 800 字符 a-run 上产生的**每一条重叠命中**都与朴素 oracle 逐条比较。

### 首轮失败与修复（如实记录）

第一次 `./run-tests.sh` 有 7 处失败，均为测试预期/夹具问题，修复后全绿：

1. `core-aho-corasick`：经典 `ushers` 样例我最初误写“hers 不命中”。
   实际 AC 在 `ushers` 上有 3 条命中：`she@1-4`、`he@2-4`（dictionary link 后缀输出）、
   `hers@2-6`。修正预期为 3 条（引擎输出本来就是对的，fuzz 1200 次差分也佐证）。
2. `unicode-boundaries`：第二个 emoji 的 `charStart` 预期算错。
   文本 `a😀b你好😀c`，第 2 个 😀 的 code point 起点是 5，UTF-16 单元起点是
   1(a)+2(代理对)+1(b)+1(你)+1(好)=6，不是 7。修正预期。
3. `server-e2e`：手写 JSON 解析器把整数解析为 `Long`，测试里用 `long`/`int`
   字面量比较导致类型不相等（`1L` vs `Double`?——实际是 `Long` vs `Integer`）；
   统一改为 `((Number) …).intValue()`。另一个断言“第一块 0 命中”忽略了模式 `bc`
   在第一块 `abc` 内就完成，改为断言 1 条且 id 为 `bc`，第二块再断言 `abcde` 跨块。

另外首轮测试 JVM **挂住不退出**（主线程已跑完，CPU 空闲）。`jstack` 显示主线程处于
`DestroyJavaVM` 等待，存活的是 `HttpServer` 固定线程池的 8 个**非守护**线程——
`HttpServer.stop()` 不会关闭外部 set 进去的 executor。修复：把 executor 存为字段，
`stop()` 中追加 `executor.shutdownNow()` + `awaitTermination`。修复后 4 秒正常退出。

### 验收点 → 测试覆盖

- **全部分块位置与朴素匹配比较**：
  - `CrossChunkTest`：对多组文本，在**每一个** code point 切点和**每一个** UTF-8 字节
    切点切成两半，以及所有 1..n 的块大小，流式结果与 `NaiveMatcher` 全量逐条相等；
  - `FuzzTest`：固定种子 400 轮随机（5 类字母表含 ASCII/高重复/BMP/emoji/组合符、
    随机模式含空串与随机复制的重复模式），每轮做一次性、随机 code point 分块、
    随机 UTF-8 字节分块三种差分，约 1200 次全量比较；
  - `CorpusTest`：三个合成语料 × 块大小 {1,3,7,13,64,1000} × 两种切块单位。
- **空模式策略**：`EmptyPatternTest`（ERROR 拒绝、SKIP 忽略并返回 skippedIndices、
  MATCH_EVERY_POSITION 的 n+1 边界、空流、重复空模式身份、任意分块一致性）。
- **Unicode 边界**：`UnicodeTest`（😀🚀 代理对双坐标、UTF-8 字节在四字节序列内部
  切分、e+U+0301 组合序列、跨块 astral 模式）。
- **大量共享前缀**：`SharedPrefixTest`（a^1..a^400 + a^k c 共 600 模式，
  实测 16 万+重叠命中全量相等；另 2000 随机模式在 1200 字符上冒烟 <10s）。

## 服务实跑

```
$ java -cp build/classes com.example.ac.server.HttpJsonServer 8088 127.0.0.1
streaming-aho-corasick listening on http://127.0.0.1:8088

$ ./examples/requests.sh http://127.0.0.1:8088
exit=0
```

样例脚本覆盖全部端点，关键结果（完整输出见 `/tmp/ac-requests.log`，本机临时文件）：

- `POST /api/match`，文本 `ushers abab`，模式 `he/she/hers` + 两个字面同为 `ab`
  的模式：`matchCount=7`。两个重复 ab 模式在同一位置各报一条，
  `patternIndex` 分别为 3 和 4，id 分别为 `dup-ab-1`/`dup-ab-2`——重复模式独立身份。
- 空模式：`ab` + `["a",""]` 在默认策略下 `matchCount=4`
  （a 命中 1 条 + 边界 0/1/2 空命中 3 条）；SKIP 为 1 条；ERROR 返回 HTTP 400。
- Unicode：`a😀b你好😀c` 中第一个 😀 报 `start=1,end=2,charStart=1,charEnd=3`
  （code point 与 UTF-16 双坐标不同且正确）；`你好` 报 `start=3,end=5,charStart=4,charEnd=6`。
- 语料分块：`dna` 300 字符 / 20 模式 / 每块 7 code point → 43 块、913 命中、
  **跨块完成 548 条**（`totalCrossChunk=548`，各块 `crossChunkCompleted` 之和相等）；
  `unicode` 200 code point / 每块 3 UTF-8 字节 → 179 块、536 字节、142 命中、
  跨块 25 条，字节块频繁切在 emoji 四字节序列内部，结果仍正确。
- 流式跨块：模式 `abcde`，feed `abc` 返回 0 条，feed `de` 返回
  `abcde start=0,end=5`，finish 为空。
- UTF-8 字节流：`😀x`（5 个 code point 字节：4+1）逐字节喂，模式 `😀` 在第 4 个
  字节到达时命中（`start=0,end=1,charStart=0,charEnd=2`），前 3 个字节均不报错、不产出。

### 负面用例（手动实测）

```
# 畸形字节 0xFF：立即 400
POST feed {"bytesBase64":"/w=="}  -> 400 {"error":"malformed UTF-8 input: MALFORMED[1]"}

# 截断的三字节序列（你 的前两字节 E4 BD）挂起，finish 时 400
feed {"bytesBase64":"5L0="} -> 200 (0 emitted)
finish                       -> 400 {"error":"malformed UTF-8 input: 2 trailing byte(s) at end of stream"}

未知会话 /api/sessions/nope/feed -> 404
非法 emptyPatternPolicy WAT       -> 400
畸形 JSON 体 "not-json"           -> 400
finish 后再操作同一会话           -> 404（finish 即注销）
```

## 未通过项 / 已知限制

- 无未通过项：全部自动化测试与手动样例请求均通过。
- 已知契约限制（非缺陷）：`feed(String)` 要求调用方不得把一个 UTF-16 代理对拆在两个
  chunk 中；需要任意切分时使用 `feedBytes`（UTF-8 路径自行处理跨块多字节序列）。
- 服务定位为本地库的 JSON 外壳：默认仅绑定 `127.0.0.1`，无鉴权、无持久化、
  会话数有上限（200，超出时淘汰旧会话）。
- 无前端（按要求不做）。
