"""slang — 小语言工具链库（自研词法/语法/编译/字节码/验证/解释）。

模块划分
--------
- location:   源码位置 Span
- errors:     诊断与编译期异常
- lexer:      词法分析（手写）
- ast_nodes:  AST 节点（全部带源码位置）
- parser:     递归下降语法分析（手写，核心解析不依赖任何现成编译器）
- bytecode:   栈式字节码设计、模块二进制编解码
- compiler:   AST -> 字节码（含源码位置调试映射）
- verifier:   字节码验证（跳转边界 / 栈高度与类型合流 / 局部量初始化）
- interpreter:验证后解释执行（验证通过后不应发生栈下溢）
- mutator:    单字节变异（验收用）
- service:    标准库 http.server 实现的 JSON 服务
"""

VERSION = "1.0.0"
