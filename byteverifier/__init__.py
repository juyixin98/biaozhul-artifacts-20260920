"""byteverifier —— 小语言工具链与栈式字节码验证器。

模块组成::

    common       源码位置、统一错误类型
    lexer/parser 自研词法/语法分析（不依赖任何现成编译器）
    bytecode     栈式字节码：指令、函数对象、二进制编解码
    compiler     从 AST 生成字节码
    verifier     字节码静态验证（跳转边界 / 栈高度与类型合流 / 局部量初始化）
    interpreter  通过验证后的字节码解释器
    service      零依赖 JSON HTTP 服务
    mutate       单字节变异工具
    errorpath    最短可读错误路径（CFG 上的 BFS）
"""

__version__ = "1.0.0"
