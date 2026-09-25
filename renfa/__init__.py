"""renfa —— 正则自动机引擎（教学型小语言工具链）。

子模块：
  source    源码位置（Unicode 码点偏移、行列号）
  errors    统一错误类型
  lexer     词法分析
  ast_nodes 语法树节点
  parser    递归下降语法分析（手写）
  nfa       Thompson 构造 NFA
  engine    NFA 模拟匹配（线性时间、无回溯）
  reference 简单回溯参考解释器（仅用于测试/对比）
  service   基于标准库 http.server 的 JSON 服务
"""

from .errors import RegexError
from .engine import Regex
from .nfa import NFA

__all__ = ["Regex", "RegexError", "NFA", "compile"]

__version__ = "1.0.0"


def compile(pattern: str) -> Regex:
    """编译一个正则子集模式，返回 :class:`Regex` 对象。"""
    return Regex(pattern)
