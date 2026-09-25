# LattLang：常量传播格分析工具链

一个纯后端项目：自定义一门小语言 **LattLang**，手写词法/语法分析器（保留源码
位置），构建 CFG + SSA IR，实现**稀疏条件常量传播（Sparse Conditional Constant
Propagation, SCCP，Wegman–Zadeck 1991）**，并在**保证可观察行为与错误行为不变**
的前提下做分支折叠、常量折叠与死代码消除。提供三端参考解释器（AST / SSA IR /
优化后 IR）做差分等价验证，并暴露一个无第三方依赖的 **JSON 服务**。

> 无前端。解析、IR 构建、SSA、格分析、优化均为自己实现，不依赖任何现成编译器。
> 仅使用 Python 3 标准库。

---

## 1. 目录结构

```
lattlang/
  errors.py        源码位置 Span 与结构化错误 LangError
  lexer.py         手写词法分析器（行列/偏移位置、注释）
  ast_nodes.py     AST 定义
  parser.py        手写递归下降解析器（运算符优先级、if/while）
  ir.py            SSA CFG IR 数据结构 + 文本打印/解析（往返保留位置）
  irbuild.py       AST -> 非 SSA CFG IR
  ssa.py           支配者/支配边界/φ 插入/支配树重命名（Cytron et al.）
  semantics.py     整数语义（向零截断的 / %，除零陷阱，非短路 && ||）
  analysis.py      三层格 + 双工作列表 SCCP
  optimize.py      安全优化：分支折叠/删块/常量折叠/DCE（含变更记录）
  interp_ast.py    AST 树走参考解释器（真值语义）
  interp_ir.py     SSA IR 解释器（优化前后通用，保留错误位置）
  service.py       JSON 服务（analyze / optimize / run / check）
  cli.py / __main__.py   命令行
docs/syntax.md     语言规范（词法、EBNF、语义）
examples/*.lat     验收样例（不可达分支、循环、合流、除零、副作用…）
examples/requests/*.json   JSON 服务请求样例
tests/             86+ 个自动化测试 + 随机差分模糊测试
run_tests.sh       一键运行全部测试与验收
```

## 2. 快速开始

```bash
python3 --version            # 需要 Python 3.10+（开发环境 3.12）

# 看 SSA
python3 -m lattlang.cli ssa      examples/confluence.lat
# 看 SCCP 分析结果（可达块/可执行边/常量/非常量）
python3 -m lattlang.cli analyze  examples/confluence.lat
# 看优化后的 IR（变更写到 stderr）
python3 -m lattlang.cli optimize examples/confluence.lat
# 运行：ast | ir | optimized
python3 -m lattlang.cli run      examples/loop_materializes.lat optimized
# 三端等价性检查（输出 + 错误种类/位置）
python3 -m lattlang.cli check    examples/divzero_reachable.lat
```

## 3. 语言

完整规范见 [`docs/syntax.md`](docs/syntax.md)。要点：

* 单类型任意精度整数；变量隐式初值 0；`true`/`false` = 1/0。
* `/`、`%` 向零截断；**除零/模零是运行时错误**，带精确源码位置。
* `&&`、`||` **不短路**（两侧都求值），比较与逻辑运算返回 0/1。
* `if`/`while`、`print`、赋值（`:=`），`#` 注释。

```
x := 1
if x { print 10 } else { print 20 }   # 输出 10
```

## 4. IR

打印一份看看：

```bash
python3 -m lattlang.cli ssa examples/unreachable.lat
```

* 块 `label:` 起始，顺序为从入口的 CFG 发现序；每块以终止符结束：
  `jmp L`、`br x L_true L_false`、`ret`、`unreachable`。
* 指令：

  ```
  x = const N          x = copy y          x = neg/not y
  x = add|sub|mul|div|mod|eq|ne|lt|le|gt|ge|and|or  y z
  print x
  ```

* φ：`x.2 = phi [x.1, 0]`，参数顺序与该块前驱列表顺序一一对应。
* 位置：行尾 `#@ sl:sc-el:ec off=.. len=..`，`parse_ir` 可完整读回。

## 5. 稀疏条件常量传播（SCCP）

### 5.1 三层格

每个 SSA 值一个格单元：

```
        ⊤ TOP        未定义/尚未确定
        |
   常量（每个整数一个）
        |
       ⊥ BOTTOM     非常量
```

`meet` 是平坦格的最大下界：`⊤` 是单位元，`⊥` 是零化元，两个不同常量合流为 `⊥`。

### 5.2 双工作列表，联合分析可达边

实现见 `lattlang/analysis.py`，维护两个相互驱动的工作列表：

* **flow 列表**：(块)。块**首次**到达时求值其全部 φ、指令和终止符；块**已访问
  但得到一条新的可执行入边**时，重新求值其 φ（这是经回边到达的新值得以传播的
  关键——本项目的一个专门回归测试覆盖它）。
* **ssa 列表**：格值发生下降的值。其所有使用点（φ、普通指令、`br` 条件）重新
  求值。

只有**可执行边**参与 φ 的 meet，因此绕过不可达边的常量不会与不可达值合流——
这就是“条件”那一半；常量沿 SSA def-use 边直接传播，是“稀疏”那一半。终止符
`br` 在条件为常量时只激活一条边，为 `⊥` 时两条都激活，为 `⊤` 时暂不激活。

### 5.3 安全：绝不错误折叠除零与副作用

* 抽象求值中，`div`/`mod` 的除数为**常量 0** 时返回 `⊤`（不赋常量格），该指令
  因此既不会被常量替换、也不会被 DCE 删除——运行到它时**仍然抛错**。
* 除数为非常量（`⊥`）时指令被保留（可能在运行时为 0）。
* 只有除数被证明是**非零常量**时，除法才是全函数，可自由折叠/删除。
* `print` 是副作用根，永不为常量传播而重排或删除；不可达块里的 `print` 随块
  一并删除（原本就不会执行）。

### 5.4 优化管线（`lattlang/optimize.py`）

1. 折叠 SCCP 确定的常量 `br` 为 `jmp`；条件为 `⊤`（只能经先前必陷阱路径到达）
   的块终止符改为 `unreachable`。
2. 以折叠后的 CFG 重新求结构可达，删除不可达块；**在删块之前**按旧前驱顺序
   重建 φ 参数（保证槽位不串）。
3. 用 SCCP 常量把使用点替换成立即数（遵守 5.3 的除零规则）。
4. 以 `print`、可能陷阱的 `div/mod`、终止符为根做 DCE，并删除无用 φ。
* 每步都记录结构化 `changes`（含源码 span），供服务解释“改了什么”。

## 6. JSON 服务

无 HTTP、无第三方库：从 **stdin**（或文件参数）读一个请求 JSON，向 stdout 写
一个响应 JSON。

```bash
python3 -m lattlang.cli serve examples/requests/check.json
# 或
cat examples/requests/analyze.json | python3 -m lattlang.service
```

成功：`{"ok": true, "action": ..., ...}`；
失败：`{"ok": false, "error": {"stage","message","span"?}}`。

### 请求 / 响应

| action | 请求字段 | 响应要点 |
|---|---|---|
| `analyze` | `source` | `reachable_blocks`、`executable_edges`、每块 φ/指令/终止符的格值、`lattice_summary` |
| `optimize` | `source`、`include_ir?` | `ssa_ir`、`optimized_ir`、`changes[]`、`change_count` |
| `run` | `source`、`mode`=`ast`\|`ir`\|`optimized` | `output[]`、`stdout`、`steps`、`error?`（含 span） |
| `check` | `source`、`max_steps?` | `equivalent`、三端的输出与错误签名、各自行数 |

完整字段见 `examples/requests/*.json` 与其响应。

## 7. 验收：可观察输出与错误行为一致

`check` 对每个程序比较 AST / SSA IR / 优化后 IR 三端的：

* `print` 输出序列（顺序敏感）；
* 运行时错误的阶段、消息；
* **出错运算符的源码位置**（行列/偏移）。

三类反例（都在 `examples/`，并被自动化测试覆盖）：

1. **分支不可达**：`unreachable.lat`、`divzero_unreachable.lat`、
   `side_effects.lat`——死分支（含其中的 print、除零）被删除，存活 print 顺序不变。
2. **循环**：`zero_trip_loop.lat`（常量假条件，循环体删除）、
   `loop_materializes.lat`（循环携带变量从常量变为 `⊥`，循环必须保留）。
3. **合流反例**：`confluence.lat`——运行时未知条件使两个分支都可执行，合流点
   `meet(1,2)=⊥`，两个 print 都必须保留。

错误行为反例：`divzero_reachable.lat`（可达 `1/0` 优化后仍在同一位置抛错）、
`divzero_dynamic.lat`（动态除数，除法指令保留并在首次迭代抛错）。

## 8. 测试

```bash
./run_tests.sh                 # 单元 + 集成 + 模糊，逐类汇报
# 或
python3 -m unittest discover -s tests -p 'test_*.py' -v
```

测试覆盖：词法位置、解析优先级/span、整数语义（含负数除模）、格的 meet 定律、
SSA 唯一性/支配边界/φ 放置/隐式 0 初值、IR 文本往返、SCCP（常量、不可达分支、
0 次循环、循环 `⊥`、合流 meet、除零不折叠）、优化安全（可达除零保留、动态除零
保留、非零常量可折叠、print 顺序、删块后 φ 重建）、JSON 服务（含子进程 CLI），
以及 **250 个（可配置更多）随机程序的 AST/IR/优化三端差分模糊测试**。

## 9. 已知限制 / 非目标

* 无前端、无类型系统、无函数/过程（单过程）、无数组或堆、无 I/O 之外的内建。
* 不做块合并/跳转线程化等与格分析无关的优化；优化目标是**正确演示 SCCP 与
  安全折叠**，而非极限性能。
* 解释器有步数上限（默认 1,000,000；check 服务可配 `max_steps`），用于在测试中
  优雅处理非终止程序。
