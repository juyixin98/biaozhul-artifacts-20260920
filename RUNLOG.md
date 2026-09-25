# RUNLOG — 实际运行记录

环境（命令与原始输出均来自本机实跑）：

- OS：`Linux 6.8.0-90-generic (Ubuntu)`
- JDK：`openjdk version "21.0.12.1" 2026-08-18`（`javac 21.0.12.1`）
- 无 Maven/Gradle；构建仅用 `javac`，零第三方依赖。
- 工作目录：`/home/admin/Downloads/biaozhul/opp129/b`

> 说明：本文件记录的是开发与验收当天**实际执行**的命令和结果。时间相关数据
> （如性能毫秒数、启动时间戳）在不同机器上会有差异；功能结论以测试断言为准。

## 1. 构建

命令：

```bash
mkdir -p build/classes
find src/main/java -name '*.java' > /tmp/sources.txt
javac -encoding UTF-8 -d build/classes @/tmp/sources.txt
# -> MAIN COMPILE OK
```

后续统一用脚本：

```bash
$ ./scripts/build.sh
[build] compiling main sources...
[build] compiling test sources...
[build] done -> build/classes, build/test-classes
```

构建脚本退出码 `0`。

## 2. 自动化测试

命令：`./scripts/test.sh`

最终结果（最后一轮完整运行）：

```
----------------------------------------
tests passed: 183, failed: 0
ALL TESTS PASSED
```

退出码 `0`（通过统计：`grep -c '\[PASS\]'` = 183，`grep -c '\[FAIL\]'` = 0）。

测试套件：basic segmentation、exhaustive comparison（含 dictionary version switch）、
deterministic tie-breaking、n-best paths、empty string and boundaries、
polynomial runtime、corpus loader、json round trip、http service end-to-end。

### 2.1 开发过程中测试真实发现的问题（均已修复并回归通过）

1. **Trie 节点可见性编译错误（首次编译）**：`Trie` 把私有内部类 `Node` 作为公共方法
   返回类型，无法编译。改为公共静态嵌套类 `Trie.Node` 后通过。
2. **核心 DP 松弛 bug（测试抓出，最重要的一个）**：初版把从位置 `i` 出发的**所有**
   词典边（终点可能为 `j+1 > i+1`）都放进了同一个 `next` 集合并写到 `dp[i+1]`，
   导致长词边错位、后续被覆盖。表现为“研究生命”被切成 `研究 / 究 / 生命 / 命`，
   第一轮测试 **128 过 / 53 失败**。修复为 list-Viterbi 的标准松弛：边松弛到其
   **真正终点**的原始桶，待进入该位置时（所有前驱都已松弛）再统一 prune。
   修复后重新编译运行，失败降为 2。
3. **v2 语料频率不足以体现版本切换差异**：初版 `dict_v2` 缺单字词“命”，
   “研究生|命”在统计上不可能赢过“研究|生命”（1-best 两版相同，1 个断言失败）。
   调整为 `研究生 200 / 命 60 / 研究 70 / 生命 70` 后，两版 1-best 不同且都与穷举一致。
4. **性能断言阈值对毫秒噪声过敏**：100 码点预热后仅 2ms，用它做基线得到
   1600 码点 328ms ≈ 164 倍的“假阳性”（而 400→1600 实际只增长约 19~21 倍，
   是多项式量级）。改为以 400 码点为基线（`max(t400,5ms)`）判定，避免噪声。
5. **深链同分的栈深度隐患（自查发现并加固）**：字典序决胜初版沿假设链递归比较，
   深度等于词数；服务允许长文本，极端同分时递归深度可达数千，理论上可能栈溢出。
   改为“从右向左一趟迭代、遇更靠左差异即覆盖结果”的 O(深度) O(1) 空间实现，
   并新增 4000 码点“深链同分”回归用例（所有词同代价、未知边也同价），通过。

### 2.2 “不枚举指数候选”的实测耗时

`PolynomialComplexityTest` 在**每个位置都高度歧义**的合成文本上运行 N=10
（词典含全部 1/2/3 字组合，切分空间确为指数级），最后一轮实测：

```
100 码点: 2 ms
400 码点: 14 ms
1600 码点: 320 ms
```

- 三档均远低于 3 秒上限。
- 长度 400→1600（×4）耗时增长约 23 倍，符合 DP 随长度多项式增长
  （含 Trie 匹配与每桶排序，约 O(n·L·N·log(LN))）；指数枚举在该文本上的
  增长因子约为 `2^(3*400)`，完全不可比。
- 同轮还断言 1600 码点路径完整覆盖原文、N=64 时返回 ≤64 条且代价有序，均通过。

### 2.3 小句穷举对照（验收核心）

`ExhaustiveComparisonTest` 用测试专用的穷举器枚举每个短句的**全部**切分
（空串为 1 条空路径），对 DP 的完整 N 最佳（N=64）逐条比较
**词面、未知标记、总代价**，并核对路径总数。覆盖句子包括：

- 空串、`生`、`研究`
- 重叠词：`研究生命`、`学生研究生命`、`学生会生气`、`研究生生命`、`研究学生会生气`
- 未知字符：`研究X生命`、全未知 `ひらがな`、标点 `学生，生命`
- 版本切换：`研究生命的起源` 在 `dict_v1`/`dict_v2` 上各自与穷举完整对照，
  以及混合未知句 `研究生X命`

全部一致（测试通过）。穷举器只在这些短句上运行，生产路径使用的是多项式 DP。

## 3. 服务实际启动

命令：`./scripts/run-server.sh --port 8080`

实际控制台输出：

```
loaded dict_v1.corpus as version 'dict_v1' (24 words)
loaded dict_v2.corpus as version 'dict_v2' (25 words)
segmenter service started
  listen:      http://127.0.0.1:8080
  data dir:    /home/admin/Downloads/biaozhul/opp129/b/data/corpora
  versions:    [dict_v1, dict_v2]
  default:     dict_v1
  unk cost:    10.0
  POST /segment {"text":"...","n":1,"version":"dict_v1"}
```

服务仅绑定 `127.0.0.1`，验收后已手动停止（`kill <pid>`，确认 `ps` 中无残留 Java 进程）。

## 4. 请求样例与真实响应

请求文件在 `examples/`，真实抓取的响应在 `examples/responses/`（均为 curl 实跑输出，
含 `[HTTP xxx]` 状态行）。下面摘录关键结论。

### 4.1 版本切换导致重叠词切分不同（同一文本，仅切换 version）

`POST /segment`，文本 `研究生生命`，`n=5`：

- `dict_v1` 第 1 名：`研究 / 生 / 生命`，`totalCost = 7.792473`
  （第 2 名才是 `研究生 / 生命`，9.800152；第 3 名与第 1 名词面相同但“生”按未知字走，15.194982）。
- `dict_v2` 第 1 名：`研究生 / 生命`，`totalCost = 5.059527`
  （第 2 名 `研究生 / 生 / 命`，10.90741；第 3 名 `研究 / 生 / 生命`，11.803082）。

两版 1-best 不同，且各自等于该版穷举排序的第 1 条（自动化测试已断言）。
完整响应见 `examples/responses/02-nbest-v1.response.txt`、`03-nbest-v2.response.txt`。

### 4.2 普通句（`01-basic.json`，HTTP 200）

`研究生命的起源` @ `dict_v1` → `研究 / 生命 / 的 / 起源`，`totalCost = 9.984499`，`unknownCount = 0`。

### 4.3 未知字符（`04-unknown-chars.json`，HTTP 200）

`我们学习中文分词Ψ，研究生命的起源。` →

```
我们 / 学习 / 中文 / 分词 / Ψ / ， / 研究 / 生命 / 的 / 起源 / 。
```

`Ψ`、中文逗号、中文句号均按单码点未知字切出，`token.cost = 10`、`unknown = true`，
整句 `unknownCount = 3`，`totalCost = 51.570468`。

### 4.4 空串（`05-empty.json`，HTTP 200）

`{"text":""}` → `results[0] = {"rank":1,"totalCost":0,"words":[],"tokens":[],
"tokenCount":0,"unknownCount":0}`。

### 4.5 错误样例（实跑状态码）

| 请求 | 状态码 | error |
|---|---|---|
| 不存在版本（`06-error-version.json`） | 404 | `unknown_version`（消息列出 dict_v1, dict_v2） |
| 请求体 `{bad` | 400 | `invalid_json` |
| 请求体 `{}` | 400 | `missing_field: field 'text' is required` |
| `{"text":"研究","n":0}` | 400 | `invalid_field: n must be an integer in [1, 64]` |

原始响应见 `examples/responses/error-*.response.txt` 与 `06-error-version.response.txt`。

### 4.6 管理端点

- `GET /healthz` → 200 `{"status":"ok"}`。
- `GET /versions` → 200：默认 `dict_v1`，两版词数 24 / 25，词频总和 1343 / 1485。

复现方式：启动服务后运行 `./scripts/curl-examples.sh`。

## 5. 未通过项 / 已知限制

- 最终代码状态下，**自动化测试 183/183 通过，无未通过项**。
- 开发中出现过的失败（编译可见性、DP 松弛、v2 语料频率、性能计时阈值、深链比较栈隐患）
  已在第 2.1 节如实记录并全部修复回归。
- 已知限制（设计取舍，非缺陷）：
  - 仅一元词典模型，未引入转移代价/词性/HMM 或大模型；代价完全由自建语料词频决定。
  - 合成语料规模很小（约 24/25 词），目标是算法与验收演示，不是生产级词典覆盖度。
  - N 最佳上限固定为 64；单请求文本上限 100000 码点（服务层保护）。
  - 服务无鉴权，仅监听回环地址 `127.0.0.1`，定位为本地库/本地服务。
