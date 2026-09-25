"""密钥版本审计（Key Version Audit）本地安全数据处理服务。

纯后端、本地运行：
- 密钥版本状态机：generated -> active -> retired -> destroyed
- 加密只允许使用 active 版本；解密允许 active/retired 历史版本
- 销毁（destroyed）即删除密钥材料，不可恢复
- 所有操作写入仅追加、哈希防篡改链的审计日志，日志中绝不出现密钥材料或明文
"""

from .errors import (
    AuditLogTampered,
    DecryptVersionDestroyed,
    DecryptVersionNotUsable,
    EncryptVersionNotActive,
    InvalidEnvelope,
    InvalidStateTransition,
    KeyVersionError,
    NoActiveVersion,
    VersionNotFound,
)
from .service import KeyService, VersionInfo
from .store import KeyStore

__all__ = [
    "KeyService",
    "KeyStore",
    "VersionInfo",
    "KeyVersionError",
    "VersionNotFound",
    "InvalidStateTransition",
    "NoActiveVersion",
    "EncryptVersionNotActive",
    "DecryptVersionDestroyed",
    "DecryptVersionNotUsable",
    "InvalidEnvelope",
    "AuditLogTampered",
]

__version__ = "1.0.0"
