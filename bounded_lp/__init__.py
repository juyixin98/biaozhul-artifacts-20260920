"""bounded_lp —— 两阶段单纯形线性规划求解器（纯后端）。

模块划分
--------
- :mod:`bounded_lp.errors`   异常类型
- :mod:`bounded_lp.tolerance` 数值容差与问题规模限制
- :mod:`bounded_lp.problem`  线性规划问题的数据结构与标准形预处理
- :mod:`bounded_lp.simplex`  两阶段单纯形核心（自实现，仅依赖 NumPy）
- :mod:`bounded_lp.enumerate_vertices` 顶点枚举参考实现（用于交叉核验）
- :mod:`bounded_lp.io_json` JSON 请求/响应
- :mod:`bounded_lp.cli`     命令行入口

支持的问题形式（"有界线性规划"指显式变量上界与一般线性约束的小规模 LP）::

    minimize (或 maximize)  c^T x
    s.t.  aub^T x <= b_ub,  aeq^T x = b_eq
          lb <= x <= ub          （默认 lb=0, ub=+inf，即非负变量）
"""

from bounded_lp.problem import LPProblem, StandardForm
from bounded_lp.simplex import SolveResult, solve

__all__ = ["LPProblem", "StandardForm", "SolveResult", "solve"]
__version__ = "0.1.0"
