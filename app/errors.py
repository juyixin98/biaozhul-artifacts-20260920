"""结构化验证错误。

每一次拒绝都抛出 :class:`VerifyError`，携带一个稳定的机器可读错误码
(``code``)、面向人的中文说明 (``reason``) 以及不包含任何令牌内容的
结构化上下文 (``context``)，由 HTTP 层原样返回给调用方。
"""

from __future__ import annotations

from typing import Any


class ErrCode:
    """稳定的拒绝原因码（测试与调用方依赖这些字面值）。"""

    MALFORMED_TOKEN = "malformed_token"
    MALFORMED_HEADER = "malformed_header"
    MALFORMED_PAYLOAD = "malformed_payload"
    BAD_BASE64URL = "bad_base64url"
    BAD_JSON = "bad_json"
    DUPLICATE_JSON_KEY = "duplicate_json_key"
    UNSUPPORTED_COMPRESSION = "unsupported_compression"
    MISSING_HEADER_FIELD = "missing_header_field"
    BAD_HEADER_FIELD = "bad_header_field"
    ALG_NOT_ALLOWED = "alg_not_allowed"
    HEADER_PARAMETER_FORBIDDEN = "header_parameter_forbidden"
    UNRECOGNIZED_CRIT = "unrecognized_crit"
    MISSING_PAYLOAD_CLAIM = "missing_payload_claim"
    BAD_CLAIM_TYPE = "bad_claim_type"
    UNKNOWN_ISSUER = "unknown_issuer"
    KID_NOT_FOUND = "kid_not_found"
    DUPLICATE_KID_IN_JWKS = "duplicate_kid_in_jwks"
    JWKS_FETCH_FAILED = "jwks_fetch_failed"
    JWKS_MALFORMED = "jwks_malformed"
    JWKS_KEY_INVALID = "jwks_key_invalid"
    KEY_ALG_MISMATCH = "key_alg_mismatch"
    INVALID_SIGNATURE = "invalid_signature"
    ISS_MISMATCH = "iss_mismatch"
    AUD_NOT_ALLOWED = "aud_not_allowed"
    TOKEN_EXPIRED = "token_expired"
    TOKEN_NOT_YET_VALID = "token_not_yet_valid"


class VerifyError(Exception):
    """验证失败。``loggable`` 中的字段保证不含令牌原文。"""

    def __init__(
        self,
        code: str,
        reason: str,
        *,
        context: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(reason)
        self.code = code
        self.reason = reason
        self.context = dict(context or {})

    def to_dict(self) -> dict[str, Any]:
        return {
            "error": self.code,
            "reason": self.reason,
            "context": self.context,
        }
