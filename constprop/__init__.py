"""常量传播格分析工具链（L0 小语言）。

纯后端模块，子模块：

- :mod:`constprop.source`    源码位置与错误
- :mod:`constprop.lexer`     手写词法分析
- :mod:`constprop.ast`       AST 定义
- :mod:`constprop.parser`    手写递归下降解析
- :mod:`constprop.model`     CFG / SSA IR
- :mod:`constprop.cfg`       AST -> 可化简 CFG（pre-SSA）
- :mod:`constprop.ssa`       最小 SSA 构造
- :mod:`constprop.sccp`      稀疏条件常量传播（格分析）
- :mod:`constprop.optimizer` 基于 SCCP 结果的保守优化
- :mod:`constprop.interp`    AST 参考解释器
- :mod:`constprop.ir_interp` SSA IR 解释器（优化前后同语义执行）
- :mod:`constprop.serialize` AST/IR -> JSON
- :mod:`constprop.service`   JSON HTTP 服务
- :mod:`constprop.cli`       命令行入口
"""

__version__ = "1.0.0"
LANGUAGE_NAME = "L0"
