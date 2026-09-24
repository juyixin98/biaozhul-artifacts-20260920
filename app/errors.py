"""验证过程中抛出的错误类型。

每个错误带一个稳定的机器可读 ``code``，方便 HTTP 层映射状态码、测试断言。
"""


class VerifierError(Exception):
    """元数据验证失败的基类。"""

    # 默认状态码，HTTP 层可按 code 覆盖
    http_status = 400

    def __init__(self, code: str, message: str, detail: dict | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail or {}

    def to_dict(self) -> dict:
        return {"error": {"code": self.code, "message": self.message, "detail": self.detail}}


class BadFormatError(VerifierError):
    http_status = 422


class BadSignatureError(VerifierError):
    http_status = 401


class ExpiredError(VerifierError):
    http_status = 422


class HashMismatchError(VerifierError):
    http_status = 422


class RollbackError(VerifierError):
    """试图把版本号降低到已信任版本之下（回滚攻击）。"""

    http_status = 409


class VersionGapError(VerifierError):
    """跳过中间版本（例如已信任 root v1 却直接发来 v3）。"""

    http_status = 409


class InterferenceError(VerifierError):
    """同版本号但内容被篡改，或元数据之间的绑定不一致。"""

    http_status = 409
