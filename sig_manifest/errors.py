"""统一的异常类型。

所有错误都带稳定的 ``error_code``，便于 CLI / HTTP 层输出机器可识别的结果，
也便于自动化测试精确断言（例如必须命中路径逃逸而不是其它错误）。
"""


class ManifestError(Exception):
    """本项目所有自定义异常的基类。"""

    error_code = "manifest_error"
    cli_exit_code = 1


class CanonicalJSONError(ManifestError):
    """规范化 JSON 解析 / 编码失败（重复键、非法类型等）。"""

    error_code = "canonical_json_error"
    cli_exit_code = 2


class UnsafePathError(ManifestError):
    """制品相对路径非法或试图逃逸出制品根目录。"""

    error_code = "unsafe_path"
    cli_exit_code = 3


class KeyStoreError(ManifestError):
    """密钥 / 信任库文件格式或内容有问题。"""

    error_code = "key_store_error"
    cli_exit_code = 4


class ManifestSchemaError(ManifestError):
    """清单信封或已签名字段不符合 v1 schema。"""

    error_code = "manifest_schema_error"
    cli_exit_code = 5


class UnknownKeyError(ManifestError):
    """清单上的签名没有任何一个来自信任库中的已知密钥。"""

    error_code = "unknown_key"
    cli_exit_code = 6


class SignatureError(ManifestError):
    """签名本身验证失败（签名值损坏或与已签名内容不匹配）。"""

    error_code = "bad_signature"
    cli_exit_code = 7


class DigestMismatchError(ManifestError):
    """制品文件实际摘要 / 大小与清单记录不一致。"""

    error_code = "digest_mismatch"
    cli_exit_code = 8


class ManifestFileError(ManifestError):
    """制品文件缺失、不是普通文件或存在清单外多余文件。"""

    error_code = "file_error"
    cli_exit_code = 9


class RunnerError(ManifestError):
    """通过验证后的执行阶段出错（注意：验证失败不会进入该阶段）。"""

    error_code = "runner_error"
    cli_exit_code = 10


class HTTPError(ManifestError):
    """本地 HTTP 服务请求处理错误。"""

    error_code = "http_error"
    cli_exit_code = 1
