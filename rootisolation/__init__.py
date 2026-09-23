"""区间根隔离（Interval Root Isolation）纯后端计算库。

对外入口：

- :func:`rootisolation.api.solve` —— 完整求解，返回可 JSON 序列化的字典
- :func:`rootisolation.api.solve_poly` —— 返回结构化结果对象
- :mod:`rootisolation.polyops` —— 精确有理数多项式运算
- :mod:`rootisolation.isolation` —— 实根隔离与二分细化

命令行：``python -m rootisolation <request.json>``
"""

from .api import RootResult, solve, solve_poly  # noqa: F401

__all__ = ["solve", "solve_poly", "RootResult"]
