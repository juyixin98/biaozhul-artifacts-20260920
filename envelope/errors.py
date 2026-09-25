"""异常类型。"""

from __future__ import annotations


class EnvelopeError(Exception):
    """所有本项目异常的基类。"""


class AEADAuthenticationError(EnvelopeError):
    """AEAD 认证失败：密文 / nonce / 关联数据被篡改，或密钥不匹配。"""


class InvalidFormatError(EnvelopeError):
    """容器 / 元数据的未受保护格式字段非法。"""


class TruncatedContainerError(EnvelopeError):
    """容器被截断：读到不完整的帧或魔数。"""


class CorruptContainerError(EnvelopeError):
    """容器逻辑不一致：块数、明文长度、受保护字段对不上等。"""


class KeyNotFoundError(EnvelopeError):
    """指定的主密钥不存在（可能尚未生成或已被删除）。"""


class NoMasterKeyError(EnvelopeError):
    """密钥库为空，尚无主密钥可用于包裹数据密钥。"""


class RotationError(EnvelopeError):
    """轮换请求本身非法（例如目标主密钥与当前主密钥相同）。"""
