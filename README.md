# Slang 字节码验证器（纯后端）

一个从零实现的小语言工具链：**手写词法/语法分析器 → 栈式字节码编译器
→ 字节码验证器 → 验证后解释器**，外加一个**纯标准库 JSON 服务**和
**单字节变异**验收工具。核心解析与分析全部自研，不调用任何现成
编译器或解析框架。

> 关键保证：**凡是通过验证的字节码，解释器执行时都不会发生栈下溢**
> （也不会读到未初始化局部量、跳到指令边界外）。该性质通过
> 数千个单字节变异体的自动化活动持续检验。

## 它能检查什么

验证器在每个函数上做单调数据流分析，检查：

1. **跳转边界**：目标在代码范围内、对齐到指令边界（覆盖前跳与回边）；
2. **栈高度与类型合流**：基本块合流处高度必须相等、逐格 `int/bool`
   类型必须一致；运算按签名弹压，栈下溢即拒；
3. **局部量初始化**：确定赋值分析，`LOAD` 一个“某条路径可能未赋值”
   的槽会被拒绝（合流时一路写、一路未写 => 未初始化）；
4. 调用约定（实参个数/类型、函数号）、返回约定（`RET` 空栈、
   `RETV` 类型匹配）、栈高上限、不得从函数末尾“落入虚无”。

错误以结构化形式返回，附带从函数入口到出错指令的
**最短可读错误路径**（CFG 上 BFS，节点带源码行列）。

## 目录结构

```
slang/
  location.py     源码位置 Span / 行列换算
  errors.py       诊断、编译异常、结构化 VerifyError、错误路径
  lexer.py        手写词法分析
  ast_nodes.py    AST（所有节点带 Span）
  parser.py       手写递归下降语法分析
  bytecode.py     指令集设计、模块二进制编解码
  compiler.py     AST -> 字节码（含类型检查、标签回填、调试映射）
  verifier.py     字节码验证器（worklist 数据流 + BFS 最短路径）
  interpreter.py  验证后解释执行（栈下溢=不变量破坏断言）
  mutator.py      单字节变异（targeted / exhaustive）
  campaign.py     变异活动驱动器（分类统计、查不变量破坏）
  service.py      标准库 http.server 的 JSON 服务
  __main__.py     命令行
examples/         示例程序、手工非法模块、JSON 请求样例
tests/            unittest 自动化测试（66 个）
docs/             LANGUAGE.md（语法）、BYTECODE.md（字节码与验证）
RUNLOG.md         实际运行命令与结果记录
```

## 运行环境与依赖

- Python **3.10+**（开发实测 3.12），**仅标准库**，无需安装任何包。

## 快速开始

```bash
# 直接跑源码（内部会先编译、验证，再解释执行）
python3 -m slang run examples/sum_loop.sl        # 55

# 编译成模块（hex 文本）
python3 -m slang compile examples/sum_loop.sl -o examples/sum_loop.bin

# 验证
python3 -m slang verify examples/sum_loop.bin

# 反汇编（带源码行号）
python3 -m slang disassemble examples/sum_loop.bin

# 单字节变异活动（targeted）
python3 -m slang mutate examples/sum_loop.sl

# 对极小函数做“每字节全部 255 种取值”的穷尽变异
python3 -m slang mutate examples/sum_loop.sl --strategy exhaustive --func 1

# JSON 服务
python3 -m slang serve --port 8000
```

## 错误路径长什么样

对一个越界跳转，验证器输出（CLI）：

```
[JUMP_OUT_OF_BOUNDS] 函数 f (#0) pc=0
  JUMP 目标 pc=100 越界（代码长度 3）
  最短错误路径:
  0. 进入函数 f @pc=0
  1. 到达 @pc=0: JUMP 目标 pc=100 越界（代码长度 3）
```

对“一路赋值、一路未赋值”的合流读取，会给出经过哪些
`fallthrough / JIF` 分支才到达该 `LOAD` 的最短序列，并定位到源码。
变异后调试映射仍然保留，因此路径节点能回显源码行列。

## 单字节变异验收

```bash
# 生成 7 个手工汇编的非法模块
python3 examples/make_bad_modules.py
python3 -m slang verify examples/bad/bad_oob_jump.bin
python3 -m slang verify examples/bad/bad_backedge.bin
python3 -m slang verify examples/bad/bad_stack_merge.bin
# ... 共 7 类：越界跳转/回边未对齐/栈高合流/类型合流/未初始化/下溢/返回不匹配
```

变异活动把每条变异分类为：`decode_rejected / verify_rejected /
runtime_error / ran_clean`，并统计 `verify` 错误码分布；
`invariant_broken / verifier_crashed / interpreter_crashed`
在合法基线上**必须为 0**（非零时 CLI 退出码为 4）。

覆盖的验收情形：

- **回边**：`sum_loop.sl` 的循环回边，以及变异后产生的
  `JUMP_UNALIGNED` / 合流冲突；
- **异常返回**：`early_return.sl` 的分支提前返回与递归基线返回，
  变异产生 `RETURN_MISMATCH`；
- **越界跳转**：把 `JUMP/JIF` 的 `rel16` 操作数改成极值，
  产生 `JUMP_OUT_OF_BOUNDS`。

## JSON 服务

`python3 -m slang serve`（默认 `127.0.0.1:8000`），路由：

| 路由 | 输入要点 | 成功返回 |
|------|----------|----------|
| `POST /compile` | `{source, encoding?}` | `module.binary`(hex/base64)+`disasm` |
| `POST /verify` | `{module:{binary,encoding}}` | `{ok, verified}` 或错误数组 |
| `POST /run` | `{module, fuel?}` | `{printed, return, steps, max_stack}` |
| `POST /mutate` | `{module, strategy?, limit?}` | 变异元数据列表 |
| `GET /health` | — | `{ok:true}` |

验证失败响应：

```json
{
  "ok": false,
  "stage": "verify",
  "verified": false,
  "errors": [
    {
      "code": "LOCAL_UNINITIALIZED",
      "message": "读取局部量槽 #2 ... 前，存在一条未对其赋值的控制流路径",
      "function": "main",
      "function_index": 1,
      "pc": 69,
      "src_line": 28,
      "src_col": 11,
      "shortest_error_path": [
        {"pc": 0, "kind": "entry", "src_line": 10, "src_col": 19, "note": "main"},
        {"pc": 31, "kind": "jif-taken", "target": 36, "src_line": 27, "src_col": 8}
      ]
    }
  ]
}
```

可直接用 `examples/requests/` 下的样例：

```bash
curl -s -X POST http://127.0.0.1:8000/compile \
  -H 'Content-Type: application/json' --data @examples/requests/01_compile.json
```

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖：词法/语法与源码位置、运算符优先级、合法程序验证、
手工非法字节码的各错误码、解释器语义（含向零截断除法/除零/燃料/
递归深度）、真实 HTTP 服务端到端，以及四个示例程序的变异安全
性质和一个极小函数的全量 255 取值变异。

## 语法与字节码文档

- [`docs/LANGUAGE.md`](docs/LANGUAGE.md)：完整 EBNF、类型系统、语义；
- [`docs/BYTECODE.md`](docs/BYTECODE.md)：指令集表、模块二进制格式、
  验证器格/合流规则/算法/错误码、最短错误路径与安全保证。

## 范围说明（不做前端）

本项目仅交付后端库、CLI 与 JSON API；没有任何网页/图形界面。
`int` 立即数为 16 位（受指令格式约束），运行期整数不受此限；
变量作用域简化为函数级。
