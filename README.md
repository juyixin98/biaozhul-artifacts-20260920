# diff-service — 带位置的行级 Myers 差异算法（纯后端）

零外部依赖的 Java 文本差异与本地检索库 + JSON HTTP 服务。只使用 JDK 21
内置能力（`com.sun.net.httpserver`、`java.net.http`），不调用任何外部搜索
服务或大模型；检索语料全部在代码中自建合成（`Corpus.synthetic()`）。

## 能力

1. **行级 Myers 差异**（Myers 1986, O(ND)），输出**可应用的编辑序列**
   （`equal` / `delete` / `insert`，连续同类编辑已合并为块）。
2. **换行形式与末尾换行作为内容**：`\n` 与 `\r\n` 作为行 token 的一部分
   保留；`"a\n"` 与 `"a"` 被视为不同内容；支持空文件（0 行）。
3. **带位置**：每个编辑同时给出行号区间（0 基半开）和字符偏移区间，
   可直接定位到原文；统一 diff（unified diff）hunk 带 1 基行列号与
   上下文窗口。
4. **预算限制与显式降级**：
   - `maxInputChars`：old+new 字符总数超预算时不运行 Myers；
   - `maxEditDistance`：Myers 只搜索到 D ≤ 预算的路径，超出即降级。
   - 降级时返回一个**合法、可应用且能还原目标**的贪心脚本（对齐公共行
     前缀/后缀、替换中段），并在结果中显式标记
     `degraded=true`、`shortest=false`、`degradeReason=<原因>`。
   - **不谎称最短**：只有非降级结果才声称 `shortest=true`。降级脚本可能
     恰好也是最短的（偶然），但系统不会做此声明。
5. **编辑序列校验与应用**：`/api/apply` 在应用前做结构性校验（连续性、
     不重叠、行内容与原文一致、覆盖完整），畸形脚本返回 `422`。
6. **本地检索**（`SearchEngine`）：AND 分词匹配、词频/标题加权、短语加分，
   返回命中文档的**行号与字符偏移**（“带位置”）。

## 目录结构

```
src/com/example/diff/
  Lines.java            行切分（保留换行符；记录偏移）
  Edit.java             合并后的编辑块（行号+字符偏移+行内容）
  MyersDiff.java        Myers 核心、回溯、预算、降级回退
  ApplyEdits.java       脚本校验 + 应用
  Hunk.java/HunkLine.java  统一 diff hunk 构造
  UnifiedDiff.java      统一 diff 渲染（含 \ No newline 标记）
  DiffResult.java       结果（含 degraded/shortest/degradeReason）
  Main.java             CLI 入口
  json/Json.java        零依赖 JSON 解析/序列化
  corpus/               合成语料 + 本地检索
  server/HttpServerMain.java  JSON HTTP 服务 + Dto
tests/                  自带微型测试框架的全部自动化测试（含随机性质测试）
examples/               请求样例与 CLI 样例文本
```

## 构建与测试

仅需 JDK 21（`javac`/`java`），无需 Maven/Gradle：

```bash
./build.sh
# 等价于：
# javac -d build/classes $(find src tests -name '*.java')
# java  -cp build/classes com.example.diff.RunAllTests
```

## HTTP 服务

```bash
java -cp build/classes com.example.diff.Main server 8080
```

| 方法 | 路径 | 请求体 |
|---|---|---|
| POST | `/api/diff` | `{"old","new","maxEditDistance?","maxInputChars?","context?"}` |
| POST | `/api/apply` | `{"old","edits":[...]}` |
| POST | `/api/search` | `{"query","limit?"}` |
| GET | `/api/corpus` | 合成语料清单 |
| GET | `/api/corpus/{id}` | 单篇文档 |
| GET | `/health` | 存活检查 |

`/api/diff` 响应中除编辑序列与 hunks 外，还包含：

- `degraded` / `shortest` / `degradeReason` —— 降级与最短性的诚实声明；
- `unifiedDiff` —— 渲染好的统一 diff 文本；
- `appliedOk` / `appliedEqualsNew` —— 服务端当场应用并与 `new` 校验。

请求样例（见 `examples/`）：

```bash
curl -s -X POST http://localhost:8080/api/diff \
  -H 'Content-Type: application/json' -d @examples/diff-request.json

# 强制降级（真实行距离为 4，预算 1）
curl -s -X POST http://localhost:8080/api/diff \
  -H 'Content-Type: application/json' -d @examples/diff-degraded-request.json

curl -s -X POST http://localhost:8080/api/search \
  -H 'Content-Type: application/json' -d @examples/search-request.json

curl -s -X POST http://localhost:8080/api/apply \
  -H 'Content-Type: application/json' -d @examples/apply-request.json
```

编辑对象的 JSON 形状：

```json
{"kind":"delete",
 "oldStart":3,"oldEnd":5,"newStart":3,"newEnd":3,
 "oldCharStart":62,"oldCharEnd":104,"newCharStart":62,"newCharEnd":62,
 "oldLines":["...\n"],"newLines":[]}
```

## CLI

```bash
# JSON
java -cp build/classes com.example.diff.Main diff <oldFile> <newFile> \
     [maxEditDistance] [maxInputChars] [context]
# 统一 diff 文本
java -cp build/classes com.example.diff.Main --udiff examples/old.txt examples/new.txt
```

## 测试设计（验收对应）

- **随机短文本性质测试**（`DiffPropertyTests`）：4000 组 LF + 4000 组
  CRLF 随机文本（小行母版，重复行密度高），断言
  (1) 应用编辑序列精确还原目标；(2) 不降级时距离等于独立 O(NM) LCS
  动态规划参考实现给出的最短距离；(3) 强制距离/字符预算降级后仍可应用、
  仍还原目标，且 `degraded=true, shortest=false`；(4) 预算 0 下回退对
  2000 组随机对全部适用。
- **显式边界用例**（`MyersDiffTests` / `LinesTests`）：全部相同的重复行、
  空文件↔空/非空、LF vs CRLF、末尾换行增删、孤立 CR 作为内容、无末尾
  换行标记、hunk 坐标、连续块合并、预算边界。
- **应用器校验**（`ApplyEditsTests`）：空洞、行内容不符、非法区间、
  覆盖不全等畸形脚本必须失败。
- **服务端到端**（`HttpServerTests`）：真实 JDK HTTP 服务临时端口，覆盖
  正常/CRLF/降级/畸形脚本 422/坏 JSON 400/检索/语料/404。
