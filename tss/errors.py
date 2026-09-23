"""带错误码的异常类型。

每个异常带稳定的 ``code`` 字符串，供服务层映射为 HTTP 状态码与 JSON 错误体，
测试也按错误码断言，避免按错误文案匹配。
"""


class TSSError(Exception):
    """所有 TSS 相关错误的基类。"""

    code = "tss_error"

    def __init__(self, message: str = ""):
        super().__init__(message or self.code)
        self.message = message or self.code


class ParameterError(TSSError):
    """调用参数不合法（阈值/份额数越界、密钥长度不对等）。"""

    code = "invalid_params"


class ThresholdError(TSSError):
    """可用份额不足阈值，无法恢复。"""

    code = "below_threshold"


class DuplicateIndexError(TSSError):
    """同一恢复请求中出现重复横坐标（x）。"""

    code = "duplicate_index"


class FormatError(TSSError):
    """份额编码/信封格式错误（base64、版本、域标签、长度等）。"""

    code = "malformed_share"


class IntegrityError(TSSError):
    """份额完整性标签校验失败（意外损坏或恶意篡改）。"""

    code = "integrity_failure"


class ConsistencyError(TSSError):
    """份额之间不一致（混批 split_id、恢复结果冲突等）。

    ``suspect`` 给出请求内被诊断为异常的份额下标（从 0 开始）；
    ``votes`` 给出诊断用的组合统计。无 HMAC 认证时该结果仅为启发式。
    """

    code = "inconsistent_shares"

    def __init__(self, message: str = "", suspect=None, votes=None):
        super().__init__(message)
        self.suspect = list(suspect or [])
        self.votes = votes or {}
