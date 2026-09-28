"""对称矩阵特征分解服务。

模块职责:
- config: 规模 / 容差 / 迭代预算等可配置项
- errors: 失败类别 (错误语义)
- validation: 数值输入校验 (对称性按相对容差)
- kernel: Householder 三对角化 + 隐式移位 QR (核心计算)
- evidence: 残差 / 正交性 / 子空间 / 重构证据
- service: 计算编排, 证据不达标不报成功
- api: FastAPI 接口
"""

__version__ = "1.0.0"
CORE_IMPL = "householder-tridiagonalization+implicit-shift-qrl"
