"""安全限制与错误类型。

集中放置策略结构上限（防止畸形/恶意策略导致资源耗尽），
以及策略校验、签名校验阶段抛出的错误类型。
"""
from __future__ import annotations

from typing import List

# ---- 策略结构上限（解释器自身的防护，不依赖外层网关） ----
MAX_POLICIES = 50            # 单次请求最多策略数
MAX_RULES = 500              # 展平后最多规则数
MAX_CONDITION_NODES = 2000   # 条件 AST 最多节点数
MAX_CONDITION_DEPTH = 20     # 条件 AST 最大嵌套深度
MAX_LITERAL_STRING_LEN = 1000
MAX_REQUEST_BODY_BYTES = 1 * 1024 * 1024  # HTTP 请求体上限 1 MiB


class PolicyError(Exception):
    """策略本身的问题（结构/语义非法）。"""

    def __init__(self, details: List[str] | str):
        if isinstance(details, str):
            details = [details]
        self.details: List[str] = details
        super().__init__("; ".join(details))


class BundleError(Exception):
    """签名策略包格式错误。"""


class SignatureError(Exception):
    """签名验签失败（签名不匹配或公钥不被信任）。"""
