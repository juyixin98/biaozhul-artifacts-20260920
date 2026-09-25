"""矩形装箱下界 —— 纯后端计算库。

模块一览：
* :mod:`packing.geometry`      几何原语、容差、碰撞检测（NumPy）
* :mod:`packing.bounds`        输入校验、范围限制、有效下界
* :mod:`packing.heuristics`    Bottom-Left / FFDH 启发式（非最优）
* :mod:`packing.exact`         小整数算例的候选位置 DFS 精确解
* :mod:`packing.grid_bruteforce` 独立网格暴力法（测试对照用）
* :mod:`packing.layout`        布局合法性校验
* :mod:`packing.api`           JSON 接口编排
"""

from .geometry import EPS

__all__ = ["EPS"]
