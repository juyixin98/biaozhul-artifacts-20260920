"""默认容差与规模限制。

容差说明（README 有同样的说明，两处需保持一致）：

* ``SYMMETRY_RTOL`` / ``SYMMETRY_ATOL``：对称检查。若
  ``|A - A.T| <= SYMMETRY_ATOL + SYMMETRY_RTOL * |A|`` 逐元素成立则视为对称；
  非对称输入会先被拒绝，非对称输出视为内部错误。
* ``SPD_RTOL``：正定检查（Cholesky 失败时的等价判定尺度）。
* ``PSD_FLOOR``：半正定（PSD）检查的绝对下限。协方差的最小特征值满足
  ``eig_min >= -(PSD_FLOOR + PSD_RTOL * eig_max)`` 即视为数值上半正定。
* ``COV_SYM_FLOOR``：协方差对称化的绝对下限（尺度通常远大于机器 epsilon）。
* ``MAX_ABS_VALUE``：所有输入标量元素的模上限，防止上溢。
* ``MAX_STATE_DIM`` / ``MAX_MEAS_DIM`` / ``MAX_STEPS``：小中规模问题上限。
"""

from __future__ import annotations

# 机器精度的若干倍作为相对容差
SYMMETRY_RTOL: float = 1e-9
SYMMETRY_ATOL: float = 1e-12
SPD_RTOL: float = 1e-9
PSD_RTOL: float = 1e-9
PSD_FLOOR: float = 1e-10
COV_SYM_FLOOR: float = 1e-12

# 输入范围
MAX_ABS_VALUE: float = 1e12

# 规模上限（小中规模）
MAX_STATE_DIM: int = 128
MAX_MEAS_DIM: int = 128
MAX_STEPS: int = 100_000
