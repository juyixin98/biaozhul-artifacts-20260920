# 运行记录（RUN_LOG）

记录环境、实际执行的命令与结果。时间：2026-09-24，目录：项目根目录。

## 环境

```text
$ java -version
openjdk version "21.0.12.1" 2026-08-18 (OpenJDK 64-Bit Server VM)
$ javac -version
javac 21.0.12.1
$ which mvn gradle
（均不存在 —— 本项目只用 javac，零构建工具、零第三方依赖）
```

## 1. 构建

```bash
$ scripts/build.sh
[build] 编译 src -> build/classes
[build] 编译 tests -> build/classes
[build] 完成
```

退出码 0，无警告。

## 2. 自动化测试

```bash
$ scripts/test.sh
```

| 测试类 | 用例数 | 结果 |
|---|---|---|
| SegTest（分词核心：重叠词/未知字/空串/同分决胜/N最佳/穷举对照/300 随机句） | 19 | 全部通过 |
| DictTest（词典解析、负代价、缺指令、版本切换、目录加载） | 9 | 全部通过 |
| ServiceTest（业务层参数校验、400/404 分支、JSON 往返） | 17 | 全部通过 |
| ServerTest（真实 HTTP 端到端，临时端口起停服务） | 9 | 全部通过 |
| **合计** | **54** | **全部通过，退出码 0** |

## 3. CLI 实际运行

```bash
$ java -cp build/classes com.example.seg.Main seg v1 研究生生命 4 data/dicts
#1 研究/生/生命  总代价=3  token数=3
#2 研究生/生命  总代价=5  token数=2
#3 研究/生/生/命  总代价=7  token数=4
#4 研究生/生/命  总代价=9  token数=3

$ java -cp build/classes com.example.seg.Main seg v2 研究生生命 4 data/dicts
#1 研究生/生命  总代价=6.5  token数=2
#2 研究生/生/命  总代价=7.5  token数=3
#3 研究/生/生命  总代价=10  token数=3
#4 研究/生/生/命  总代价=11  token数=4

$ java -cp build/classes com.example.seg.Main enum v1 研究生生命 data/dicts
穷举路径总数: 6
#1 研究/生/生命  总代价=3  token数=3
#2 研究生/生命  总代价=5  token数=2
#3 研究/生/生/命  总代价=7  token数=4
#4 研究生/生/命  总代价=9  token数=3
#5 研/究/生/生命  总代价=12  token数=4
#6 研/究/生/生/命  总代价=16  token数=5
```

穷举最优与 DP 最优一致（`研究/生/生命`，代价 3）。重叠词、未知字、空串、版本切换的
完整 demo 输出见构建时生成的 `build/log-demo.txt`（`Main demo`）。要点：

- `结婚的和尚未结婚的` → `结婚/的/和/尚未/结婚/的`（代价 8.5），穷举 8 条路径，DP 一致；
- `研究生命起源X` → `研究/生命/起源/X`，X 为未知字（代价 5），`known=false`；
- 空串 → 空路径，代价 0；
- v1 最优 `研究/生/生命`（3），v2 最优 `研究生/生命`（6.5）——版本切换改变最优切分。

## 4. HTTP 服务实际运行（curl）

`scripts/run-server.sh` 启动后，对每个样例实际请求（响应体完整保存于
`samples/responses/`）：

| 请求 | HTTP 状态 | 结果摘要 |
|---|---|---|
| GET `/api/dictionaries` | 200 | v1(未知代价5,16词)、v2(未知代价8,16词) |
| POST `/api/segment`（v1，研究生生命） | 200 | `研究/生/生命`，totalCost 3 |
| POST `/api/segment`（k=5，重叠词长句） | 200 | 返回 5 条，第1 `结婚/的/和/尚未/结婚/的` 8.5 |
| POST `/api/segment`（v2，同句） | 200 | `研究生/生命`，totalCost 6.5（版本切换） |
| POST `/api/segment`（含 X） | 200 | X 为未知 token，known=false，cost 5 |
| POST `/api/segment`（空串） | 200 | tokens=[]，totalCost 0 |
| POST `/api/crosscheck`（结婚的和尚未，k=10） | 200 | totalSegmentations=4，match=true，mismatches=[] |
| 版本不存在 | **404** | `DICT_NOT_FOUND` |
| k=0 | **400** | `BAD_K` |
| 非法 JSON | **400** | `INVALID_JSON` |
| GET 打 `/api/segment` | **405** | `METHOD_NOT_ALLOWED`（ServerTest 覆盖） |

## 5. 过程中出现过的问题（如实记录，均已修复）

1. **词典注释被误判为指令**：最初解析器把说明行
   `# @unknown-cost 未登录单字的统一代价（非负整数）` 当成指令去解析整数，首次运行
   `Main demo` 直接抛 IOException。修复：只有指令后确实跟非负整数时才视为指令，
   否则按普通注释忽略（新增回归测试
   "`@unknown-cost` 说明文字行不误判，真正指令生效"）。
2. **合成词典成本初次设计不产生版本差异**：最初 v1/v2 的词条使两句最优切分相同。
   手工逐路径核算（5 字串 研/究/生/生/命）后，补入单字 `生` 并重设代价，得到
   v1=`研究/生/生命`(3) 与 v2=`研究生/生命`(6.5) 的确定性差异。
3. **首批测试 2 个断言失败（测试自身类型问题，非算法问题）**：
   - `DictTest` 用 `LinkedHashMap.keySet()` 与 `List.of` 调 `equals`，元素相同但集合
     类型不同导致失败 —— 改为 `List.copyOf(...)` 后通过；
   - `ServerTest` 把 JSON 数字（解析为 `BigDecimal`）与 `Integer 1` 直接比较 —— 改为
     `intValueExact()` 后通过。
   
   修复后重新干净构建并跑全量测试：**54/54 全部通过，无未通过项**。

## 6. 未做的事项

- 未实现前端（按要求只交付 CLI 与 JSON 接口）。
- 未接入任何外部搜索服务或大模型；未使用真实世界词典/语料，全部为合成数据。
- 穷举分词器刻意限制 ≤16 字并在超长时返回 400，它只用于小句对照，不是生产路径。
