"""taintflow —— 自研小语言工具链 + 跨函数污点分析库与 JSON 服务。

模块概览：
  lexer / parser : 手写词法与递归下降语法分析（核心解析不依赖现成编译器）
  ir             : 自研低层 IR（标签基本块 + 线性指令）
  taint_model    : 污点格、符号路径轨迹、过程内 CFG 数据流
  analyzer       : 有限上下文摘要的过程间污点分析（单调不动点）
  service        : 纯 JSON HTTP 服务（标准库 http.server，无第三方依赖）
  cli            : 命令行入口

入口 API 见 :func:`taintflow.service.analyze_source`。
"""

from .version import __version__
from .config import AnalysisConfig
from .errors import TaintflowError, LexError, ParseError, AnalysisError
from .service import analyze_source

__all__ = [
    "__version__",
    "AnalysisConfig",
    "TaintflowError",
    "LexError",
    "ParseError",
    "AnalysisError",
    "analyze_source",
]
