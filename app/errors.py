"""业务错误类型，API 层统一映射为 HTTP 状态码。"""
from __future__ import annotations


class SlasherError(Exception):
    status_code = 400
    code = "bad_request"

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        status_code: int | None = None,
    ):
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code
        if status_code is not None:
            self.status_code = status_code


class NotFound(SlasherError):
    status_code = 404
    code = "not_found"


class Conflict(SlasherError):
    status_code = 409
    code = "conflict"


class InvalidVote(SlasherError):
    status_code = 422
    code = "invalid_vote"
