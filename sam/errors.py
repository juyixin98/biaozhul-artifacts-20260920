"""SAM 各阶段统一的错误类型。

验证编排 (sam.verify) 不会把这些异常直接抛给调用方, 而是转成
VerifyProblem 列表收集起来; 其它环节 (签名/CLI 直接调用) 直接抛出。
"""


class SAMError(Exception):
    """所有 SAM 错误的基类。"""


class CanonicalJSONError(SAMError, ValueError):
    """规范化 JSON 解析或序列化失败 (重复键 / 非法类型 / BOM 等)。"""


class UnsafePathError(SAMError):
    """清单项路径试图逃逸制品根目录。"""


class UnsupportedFileTypeError(SAMError):
    """制品目录中存在不支持的特殊文件 (FIFO/设备/套接字/目录软链等)。"""


class KeyStoreError(SAMError):
    """密钥或信任库加载失败。"""


class UnknownKeyError(SAMError):
    """签名所用 key_id 不在本地信任库中。"""


class UnsupportedAlgorithmError(SAMError):
    """封套声明了本版本不支持的签名算法。"""


class ManifestError(SAMError):
    """清单/封套结构不符合 schema。"""


class SignatureError(SAMError):
    """签名校验失败。"""


class ServiceError(SAMError):
    """本地 HTTP 服务请求错误。"""
