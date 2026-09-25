"""硬性限制（输入范围）与默认值。

精确算术的代价随系数位数与二分深度增长，因此显式限定问题规模；
触及上限时不强行给答案，而是返回明确的失败状态（见 engine.py）。
"""
from fractions import Fraction

# 多项式次数上限（小中规模）
MAX_DEGREE = 100

# 单个（约分后）系数分子/分母的二进制位数上限
MAX_COEFF_BITS = 4096

# 每次隔离 / 细化二分深度上限（请求可调，不得超过硬上限）
MAX_DEPTH_DEFAULT = 5000
MAX_DEPTH_LIMIT = 10000

# 细化目标宽度 epsilon 的允许范围
EPS_DEFAULT = Fraction(1, 10**12)      # 1e-12
EPS_MIN = Fraction(1, 10**200)         # 拒绝假精度：再窄没有意义且代价失控

# 十进制输出最多保留的小数位数（含误差半径上取整）
MAX_DECIMAL_DIGITS = 256
