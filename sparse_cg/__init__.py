"""稀疏（预条件）共轭梯度纯后端库。

只依赖 Python 标准库与 NumPy，面向小、中规模对称正定（SPD）稀疏线性系统。
"""

from .csr import CSRMatrix, check_symmetric
from .errors import RequestError
from .io_api import solve_request
from .pcg import PCGResult, pcg

__all__ = [
    "CSRMatrix",
    "RequestError",
    "PCGResult",
    "pcg",
    "check_symmetric",
    "solve_request",
]
