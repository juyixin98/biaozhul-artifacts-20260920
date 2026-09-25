"""加密范围读取本地服务（Encrypted Range Store）。

固定块 AEAD 对象存储格式，支持跨块 HTTP 风格范围读取：
每一块必须先通过 AES-GCM 认证，其明文才允许返回。
"""

from .errors import (
    ERSError,
    IntegrityError,
    InvalidObjectId,
    InvalidRangeHeader,
    ObjectNotFound,
    RangeNotSatisfiable,
)
from .format import (
    DEFAULT_BLOCK_SIZE,
    KEY_LEN,
    Manifest,
    ObjectReader,
    generate_master_key,
    open_object,
    write_object,
)
from .service import EncryptedObjectStore, parse_range, validate_object_id

__all__ = [
    "DEFAULT_BLOCK_SIZE",
    "KEY_LEN",
    "ERSError",
    "EncryptedObjectStore",
    "IntegrityError",
    "InvalidObjectId",
    "InvalidRangeHeader",
    "Manifest",
    "ObjectNotFound",
    "ObjectReader",
    "RangeNotSatisfiable",
    "generate_master_key",
    "open_object",
    "parse_range",
    "validate_object_id",
    "write_object",
]
