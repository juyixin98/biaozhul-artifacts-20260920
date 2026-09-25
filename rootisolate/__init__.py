"""区间根隔离（interval real-root isolation）纯后端计算库。

公开接口：
    solve(request: dict) -> dict     字典进 / 字典出的 JSON 风格核心接口
    EngineError                      可定位错误码的异常类型

所有根隔离与计数均在精确有理数（fractions.Fraction）上完成；
NumPy 仅作为 object 数组容器与逐向量化算子使用，不参与浮点近似。
"""
from .engine import EngineError, solve

__all__ = ["solve", "EngineError", "__version__"]
__version__ = "1.0.0"
