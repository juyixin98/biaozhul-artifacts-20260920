"""阈值份额恢复（Threshold Secret-Sharing，TSS）纯后端服务。

基于成熟有限域 GF(2^8) 的 Shamir 秘密分享：
- 参数与份额二进制格式均带版本号；
- 检测重复横坐标；
- 完整性标签在有独立认证密钥时为 HMAC-SHA256，否则为普通 SHA256 校验和。
"""

from .errors import (
    TSSError,
    ThresholdError,
    DuplicateIndexError,
    FormatError,
    IntegrityError,
    ParameterError,
    ConsistencyError,
)
from .gf import GF256
from .shamir import SplitParams, SharePoint, split_secret, combine_points, diagnose_points
from .envelope import TSS1_MAGIC, FIELD_RIJNDAEL, RawShare, encode_share, decode_share
from .service import ServerConfig, run_server

__all__ = [
    "TSSError",
    "ThresholdError",
    "DuplicateIndexError",
    "FormatError",
    "IntegrityError",
    "ParameterError",
    "ConsistencyError",
    "GF256",
    "SplitParams",
    "SharePoint",
    "split_secret",
    "combine_points",
    "diagnose_points",
    "TSS1_MAGIC",
    "FIELD_RIJNDAEL",
    "RawShare",
    "encode_share",
    "decode_share",
    "ServerConfig",
    "run_server",
]
