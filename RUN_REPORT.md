# 运行报告（RUN REPORT）

- 日期：2026-09-24（CST）
- 环境：Ubuntu，OpenJDK `21.0.12.1`；无 Maven/Gradle/网络依赖，全部使用 JDK 自带工具。
- 代码规模：26 个 Java 文件（主 19 + 测试 7），约 2,450 行。
- 结论：**全部自动化测试通过（37,761 个断言，0 失败，退出码 0）；
  验收穷举 9,408 个组合，朴素/优化结果均与独立集合运算真值一致。无未通过项。**

本报告如实记录执行过的命令、输出，以及开发过程中实际出现过的失败与修复
（见"过程中出现并修复的问题"一节——这些问题在最终版本均已被回归测试覆盖）。

## 1. 构建

命令：

```bash
./build.sh
```

实际输出：

```
主源码编译完成 -> build/classes
```

退出码 0，无编译警告级别的输出。

## 2. 自动化测试

命令：

```bash
./test.sh
```

最终完整输出归档于 `docs/test-output-2026-09-24.txt`。关键数据：

```
########## ParserTest ##########
   断言数: 32，失败数: 0
########## IndexTest ##########
   断言数: 29，失败数: 0
########## JsonTest ##########
   断言数: 15，失败数: 0
########## ExhaustiveEquivalenceTest ##########
   查询形态数: 588，删除子集数: 16，比较组合数: 9408
   完成比较组合: 9408
   朴素探测总数:   9024
   优化探测总数:   5868（节省 3156 次）
   严格减少探测的(查询,删除子集)组合数: 2190
   断言数: 37643，失败数: 0
########## OptimizationCostTest ##########
   pet AND cat AND zebra            朴素探测=12  优化探测=1   节省=11 命中=[]
   pet AND (falcon OR glacier)      朴素探测=7   优化探测=1   节省=6  命中=[]
   cat AND dog AND food             朴素探测=9   优化探测=8   节省=1  命中=[3, 12]
   断言数: 10，失败数: 0
########## SearchServerTest ##########
   断言数: 32，失败数: 0

全部测试套件通过 ✔      # 进程退出码 0
```

合计 32+29+15+37,643+10+32 = **37,761 个断言**。

### 验收点对照

| 任务要求 | 落实位置 | 结果 |
|---|---|---|
| 穷举小文档集合对照集合运算 | `ExhaustiveEquivalenceTest`：4 篇文档、文档-词矩阵，独立真值用 JDK 集合运算实现，不经本项目索引/求值器 | 9,408 组合三方（朴素/优化/真值）全等 ✔ |
| 覆盖纯 NOT | 查询形态含 `NOT x`、`NOT NOT x`、`NOT (…)`；点名断言 `NOT a`、`NOT z` | ✔ |
| 覆盖未知词 | 词表外的 `z`；`a AND z=∅`、`a OR z=a`、`NOT z=全集` | ✔ |
| 覆盖删除文档 | 穷举全部 16 种删除子集；另有删除 doc 11 后 NOT 全集缩小的端到端验证 | ✔ |
| 比较优化前后结果 | 每个组合同时跑朴素/优化两版；断言结果相等且优化探测数 ≤ 朴素；真实语料上给出精确探测数对照 | 结果全等；探测 9,024 → 5,868 ✔ |
| 保留错误位置 | 查询与 JSON 解析异常都带 0 基 position，HTTP 错误体透传 | ParserTest/JsonTest/端到端均断言 ✔ |

## 3. HTTP 服务实际运行记录

命令（`samples/requests/*.json` 为请求样例，`samples/responses/*.json` 为实际抓取响应）：

```bash
./run.sh --port 8080
curl -s http://127.0.0.1:8080/health
curl -s -X POST http://127.0.0.1:8080/search -H 'Content-Type: application/json' \
     -d '{"query":"pet AND cat AND zebra"}'
curl -s -X DELETE http://127.0.0.1:8080/documents/11
```

完整交互记录归档于 `docs/http-demo-2026-09-24.txt`。关键实测：

- 健康检查 `HTTP/1.1 200 OK`，体 `{"status":"ok"}`。
- `pet AND cat AND zebra`：两版计划 `resultsIdentical=true`、命中 0；
  成员探测 **朴素 12 次 vs 优化 1 次**（`probeDelta=-11`）。
- 缺右括号 `(cat AND dog`：`HTTP/1.1 400 Bad Request`，
  体 `{"error":"查询语法错误: 缺少右括号，实际遇到 ''（位置 12）","position":12}`。
- 初始 12 篇时 `NOT cat` 命中 6 篇 `[2,5,8,9,10,11]`；
  **删除文档 11 后**：`/stats` 显示 `documents=11, deletedDocuments=1, totalDocumentsEver=12`，
  `NOT cat` 的 `universeSize=11`、命中变为 `[2,5,8,9,10]`；
  被删文档独有的 `zebra` 再查命中 0。与"NOT 相对固定（且随删除收缩的）全集"语义一致。

## 4. 过程中出现并修复的问题（如实记录）

以下问题在开发中由测试或实际 curl 发现，均已修复并有测试防回归：

1. **CJK 字符被词法分析器静默吞掉**：最初用 `Character.isLetterOrDigit`
   判断词字符，而该方法对中文返回 true，导致查询 `猫` 不报错。
   修复：词字符显式限定为 ASCII 字母/数字/下划线（`QueryLexer.isWordChar`），
   其他字符报"无法识别的字符"并给出位置。回归：ParserTest 中 `assertError("猫", 0)`。
2. **JSON 整数被解析成 Double**：`isDouble ? Double.valueOf(s) : Long.valueOf(s)`
   三元表达式的两个包装类型分支被 Java 统一拓宽为 `double`，整数经 `l2d`
   装箱成 Double（通过反汇编字节码定位）。修复：改为显式 `if/else` 分别返回。
   回归：JsonTest 断言 `42` 解析为 `Long`、record 数字字段往返保持整数。
3. **record 被序列化成 toString 字符串**：`hits` 中的 `DocumentView`
   最初输出为 `"DocumentView[id=2, title=...]"` 字符串而非 JSON 对象。
   修复：JSON 序列化器通过反射识别 record 组件，输出 `{"id":..,"title":..}`。
   回归：JsonTest 增加 record 用例；`samples/responses/*.json` 已重新抓取确认。
4. 若干**测试预期笔误**（错误位置 0 基偏移算错、HTTP 数字按 Double 比较、
   新增文档后全集大小应为 13）——属测试代码问题，已按语义改正，非产品缺陷。
5. 一次文件编辑误删 `readNumber` 结尾的返回语句，随即恢复，未进入任何提交状态。

最终版本中这些问题均不存在；当前工作区干净度可由 `./test.sh` 与本报告复现。

## 5. 未通过项

无。最终运行全部通过；未实现/未尝试的功能仅限任务范围外的内容（前端、外部检索服务、
大模型调用——按要求均不做）。
