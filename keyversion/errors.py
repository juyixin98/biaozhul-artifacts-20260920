"""错误类型定义。所有业务异常继承 :class:`KeyVersionError`。"""

from __future__ import annotations


class KeyVersionError(Exception):
    """所有密钥版本相关错误的基类。"""

    # HTTP 层使用的错误码与默认状态码
    code: str = "internal_error"
    http_status: int = 500


class VersionNotFound(KeyVersionError):
    """请求的密钥版本不存在。"""

    code = "version_not_found"
    http_status = 404


class InvalidStateTransition(KeyVersionError):
    """试图执行当前版本状态不允许的状态转换。"""

    code = "invalid_state_transition"
    http_status = 409


class NoActiveVersion(KeyVersionError):
    """加密时不存在 active 版本。"""

    code = "no_active_version"
    http_status = 409


class EncryptVersionNotActive(KeyVersionError):
    """指定版本加密但该版本不是 active（generated/retired/destroyed 一律拒绝）。"""

    code = "encrypt_version_not_active"
    http_status = 409


class DecryptVersionDestroyed(KeyVersionError):
    """目标版本已 destroyed，密钥材料已被删除，不可恢复、不可解密。"""

    code = "decrypt_version_destroyed"
    http_status = 409


class DecryptVersionNotUsable(KeyVersionError):
    """目标版本存在但不处于可解密状态（如 generated：从未用于加密）。"""

    code = "decrypt_version_not_usable"
    http_status = 409


class InvalidEnvelope(KeyVersionError):
    """密文封装格式损坏或认证标签校验失败。"""

    code = "invalid_envelope"
    http_status = 400


class AuditLogTampered(KeyVersionError):
    """审计日志哈希链校验失败（被截断或被篡改）。"""

    code = "audit_log_tampered"
    http_status = 500
