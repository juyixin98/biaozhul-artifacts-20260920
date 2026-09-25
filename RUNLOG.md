# RUNLOG — 实际运行记录

本文件记录本次开发中**实际执行**的命令与结果。环境：

```
$ python3 --version
Python 3.12.3
```

操作系统：Linux（6.8 内核）。除 Python 标准库外**未安装任何第三方包**；
词法/语法/IR/分析全部为本仓库手写实现，未调用任何现成编译器或解析器框架。

> 说明：开发过程中发现并修复了若干真实缺陷（见文末“调试中发现并修复的缺陷”），
> 下列结果均为**修复后**的最终运行结果。

---

## 1. 自动化测试

命令：

```bash
python3 -m unittest discover -s tests -v
```

结果：**73 个用例全部通过，0 失败、0 错误**。

```
Ran 73 tests in 0.6s
OK
```

测试文件与覆盖点：

- `tests/test_lexer.py`（8）：数字/标识符/关键字、双字符运算符、字符串与布尔、
  注释跳过、1-based span、非法字符、未终止字符串/块注释。
- `tests/test_parser.py`（12）：空程序拒绝、参数、重复形参、优先级爬升、
  括号改写优先级、一元绑定、if/else、while 与空 return、嵌套调用、
  缺分号报错、“未声明变量不是语法错误”、节点 span。
- `tests/test_builder.py`（13）：source/sink 降级为一等指令、
  用户函数调用成为带继续块的终结符、分支结构、循环回边、
  未声明变量/重复函数/遮蔽标记/arity 错误、不透明调用降级、
  标记 arity、**指令 UID 全局唯一**、span 源码切片、嵌套实参。
- `tests/test_analyzer.py`（31）：直接污点、常量干净、强清洗、
  清洗不回溯污染源、二元运算传播、跨函数返回值/形参/三层栈帧、
  净实参、不同调用上下文、**k=2 精确 vs k=0 合并的已知误报**、
  两分支都清洗、**条件清洗的已知误报**、单侧污点、循环内污点、
  每轮清洗、**零迭代分支的已知误报**、直接/相互递归终止、k=null 封顶终止、
  不透明调用、入口形参污点开关、路径顺序/文本、多源多汇。
- `tests/test_service.py`（9）：payload 逻辑（缺字段/非对象/程序错误）、
  真实 socket 上的 `/health`、200、**400 ParseError**、**422 BadJson/BadRequest**、
  404、`include_ir:false`。

## 2. 验收样例程序（k=2, entry=main）

命令（每个文件头注释写明预期）：

```bash
for f in basic cross_function sanitized conditional_sanitize recursion \
         context_precision loop_and_branch opaque_call; do
  python3 -m taintlang.cli analyze examples/programs/$f.tl --no-ir --summary --entry main
done
```

实际输出：

```
===== basic.tl =====
findings: 1  contexts: 1  block steps: 1  flow nodes: 6 edges: 2
  ALARM sink @ 4:3  <- source @ 3 (paths=1)

===== cross_function.tl =====
findings: 2  contexts: 5  block steps: 13  flow nodes: 30 edges: 25
  ALARM sink @ 14:3  <- source @ 5 (paths=1)
  ALARM sink @ 21:3  <- source @ 5 (paths=3)

===== sanitized.tl =====
findings: 1  contexts: 2  block steps: 3  flow nodes: 8
  ALARM sink @ 10:3  <- source @ 7 (paths=2)
  # 另一个 sink（第 9 行，sink(y)）在 sinks_without_taint 中（干净）

===== conditional_sanitize.tl =====
findings: 1  contexts: 1  block steps: 4  flow nodes: 16 edges: 17
  ALARM sink @ 11:3  <- source @ 5 (paths=8)
  # 已知误报：仅 true 分支清洗，条件不解释，合流仍报

===== recursion.tl =====
findings: 1  contexts: 4  block steps: 20  flow nodes: 54 edges: 45
  ALARM sink @ 16:3  <- source @ 14 (paths=7)

===== context_precision.tl =====
findings: 1  contexts: 3  block steps: 5  flow nodes: 21 edges: 11
  ALARM sink @ 12:3  <- source @ 11 (paths=2)
  # 第 15 行 sink(c) 干净（两个调用点上下文被区分）

===== loop_and_branch.tl =====
findings: 1  contexts: 1  block steps: 12  flow nodes: 27 edges: 16
  ALARM sink @ 14:3  <- source @ 10 (paths=2)

===== opaque_call.tl =====
findings: 1  contexts: 1  block steps: 1  flow nodes: 12 edges: 3
  ALARM sink @ 8:3  <- source @ 7 (paths=1)
  # 第 11 行 sink(b) 干净；opaque_calls: external(True), external(False)
```

判定对照（与文件头预期一致）：

| 程序 | 预期告警 | 实际 | 干净 sink |
|---|---|---|---|
| basic | 1 | 1 | — |
| cross_function | 2 | 2 | — |
| sanitized | 1（原始变量） | 1 | 第 9 行 |
| conditional_sanitize | 1（**已知误报**） | 1 | — |
| recursion | 1 | 1 | — |
| context_precision (k=2) | 1 | 1 | 第 15 行 |
| loop_and_branch | 1 | 1 | — |
| opaque_call | 1 | 1 | 第 11 行 |

### 2.1 上下文深度对照（同一程序，改 k）

```bash
python3 -m taintlang.cli analyze examples/programs/context_precision.tl \
    --no-ir --summary --entry main --k 0
# findings: 2  contexts: 1        <- 第 15 行 sink(c) 也被报：0-CFA 合并导致的已知误报
python3 -m taintlang.cli analyze examples/programs/recursion.tl \
    --no-ir --summary --entry main --k 0
# findings: 1  contexts: 1        <- 仍正确，且上下文数减少
```

### 2.2 递归终止性（k=null，硬性封顶 16 个上下文）

对 50 层相互递归（isEven/isOdd）实际运行：

```
findings= 1 contexts= 16 steps= 98 truncated_ctx= 0
```

分析在上下文数到达上限后合并并终止，未挂起；sink 仍正确告警（不漏报）。

## 3. 一条源→汇路径（cross_function，第 14 行 sink）

```
sink line 14 <- source line 5 paths= 1
  source       L5   source()
  exit         L5   return from get_data
  call-return  L9   return from get_data at call #0
  block-entry  L9   block wrap.cont0
  copy         L9   q = %0
  exit         L10  return from wrap
  call-return  L18  return from wrap at call #1
  block-entry  L18  block main.cont0
  copy         L18  a = %1
  call-arg     L19  argument #0 at call #2
  param        L13  bind param x of helper
  block-entry  L13  block helper.entry
  sink         L14  sink(x)
```

路径为**正向、从源到汇、按调用上下文跨函数**，每步带 kind/label/context/源码位置。

## 4. 错误处理（退出码与分类）

```bash
echo 'func main( {' > /tmp/bad.tl
python3 -m taintlang.cli analyze /tmp/bad.tl        # 退出码 2 -> ParseError
printf 'func f(a){return a;}\nfunc main(){sink(f(1,2));}\n' > /tmp/arity.tl
python3 -m taintlang.cli analyze /tmp/arity.tl --entry main  # 退出码 2 -> BuildError
```

实际分类：`ParseError`、`BuildError`，错误 JSON 均携带精确 `span`（行列 + 文本）。

## 5. JSON/HTTP 服务实测

```bash
python3 -m taintlang.cli serve --port 8097 &
curl -s http://127.0.0.1:8097/health
curl -s -X POST http://127.0.0.1:8097/analyze \
     -H 'Content-Type: application/json' \
     --data @examples/requests/analyze_basic.json
```

实际结果：

```
GET  /health                                  -> 200 {"status":"ok","version":"1.0.0"}
POST /analyze (analyze_basic.json)            -> 200 ok=true  findings=1
POST /analyze (analyze_k0_context_merge.json) -> 200 ok=true  findings=2（含 1 个已知误报）
POST /analyze (analyze_custom_markers.json)   -> 200 ok=true  findings=1, safe sinks=1
POST /analyze  body="{bad"                    -> 422 BadJson
POST /analyze  body={}                        -> 422 BadRequest (缺 'source')
POST /analyze  body={"source":"func main("}   -> 400 ParseError（带 span）
GET  /nope                                    -> 404
```

对应请求样例：`examples/requests/analyze_basic.json`、
`analyze_k0_context_merge.json`、`analyze_custom_markers.json`；
真实响应已保存为 `examples/requests/response_basic.json`、
`response_k0.json`、`response_custom.json`。

## 6. 已知局限 / 未通过项

- **没有“未通过”的验收项**：上述全部命令返回符合预期；测试 73/73 通过。
- 分析器**有意保留的误报**（不是缺陷，是 README“保守近似”表中声明的取舍）：
  1. 条件清洗：`conditional_sanitize.tl` 的单分支清洗会告警；
  2. k=0 上下文合并：`context_precision.tl --k 0` 多报一个干净 sink；
  3. 循环 0 次迭代不可区分：污点仅在循环体内清洗时循环后仍报
     （测试中以 `test_zero_iteration_branch_is_known_false_alarm` 固化）；
  4. 不透明调用在脏实参下一律假定返回值带污点。
- 非目标（明确不做）：真实语言前端、前端 UI、别名/堆/指针、过程值/反射、
  异常与并发建模，以及“对任意图灵完备语言完备”的任何声明。

## 7. 调试中发现并修复的缺陷（如实记录）

开发自测阶段实际暴露并修复：

1. **干净环境的块不入队**：worklist 最初只在环境含污点时入队，导致无污点入边、
   但函数体内自身调用 `source()` 的块不被访问。修复：每个新可达块至少处理一次。
2. **`return 调用(...)` 的返回值丢失**：`return source();` 被降级为
   `Ret(value=None)`（终结块绑定错误）。修复降级时对已终结块的处理后，
   返回寄存器正确传入 `Ret`。
3. **指令 UID 按函数计数导致流图节点碰撞**：不同函数的 `("instr",0)`
   在全局流图上互相覆盖，跨函数污点丢失。修复：指令 UID 与调用点 UID
   改为全程序全局唯一分配（并有回归测试）。
4. **环境合并不回写**：`_enqueue` 算出并集后未写回 `self.env`，且“是否变化”
   误与新集合自身比较，导致返回污点在调用继续块处消失。修复比较旧集合并回写。
5. **合流/回边上的污点来源链断裂**：循环内引入的污点能在环境中到达 sink，
   却重建不出路径（block-entry 没有从前驱寄存器生产者连边）。
   修复：跳转/分支按“每个带污点寄存器”把其生产者连到后继块入口。
6. **控制边被误当成污点边**：纯控制可达边（空标签）会让“仅在一条分支清洗”
   的干净值经由控制边形成伪路径。修复：**每条流图标注它实际传播的 origin 集合**，
   路径 DFS 只走携带目标 origin 的边。这一改动同时精确化了条件清洗场景
   （环境层仍按 may 合流报告，路径层不再给出伪造传播链）。
