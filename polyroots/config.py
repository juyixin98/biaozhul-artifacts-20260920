"""默认数值常量。所有容差都可在 SolveOptions 中显式覆盖。"""

from __future__ import annotations

import os

# 收敛：Aberth 单根相对校正量 / 相对残差阈值
DEFAULT_CONVERGENCE_TOL = 1e-12
# 聚类：相对根间距小于该值视为近重根候选
DEFAULT_CLUSTER_TOL = 1e-6
# 共轭配对：|z - conj(w)| / (1+|z|) 小于该值才承认共轭关系
DEFAULT_CONJUGATE_TOL = 1e-8

DEFAULT_MAX_ITERATIONS = 200
# 资源上限：防止超大输入拖垮服务，可随机器调整
DEFAULT_MAX_DEGREE = int(os.environ.get("POLYROOTS_MAX_DEGREE", "256"))

# 高精度参考默认精度（十进制位）
DEFAULT_REFERENCE_DPS = 60

# Aberth 初值使用固定种子，保证同输入可重放
DEFAULT_ABERTH_SEED = 20260927

# 运行日志目录
DEFAULT_LOG_DIR = os.environ.get("POLYROOTS_LOG_DIR", "logs/runs")
