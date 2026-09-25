"""异常类型。

所有 IntegrityError 都表示：存储内容未通过认证，服务不得返回任何相关明文。
"""


class ERSError(Exception):
    """本项目所有异常的基类。"""


class ObjectNotFound(ERSError):
    """对象不存在。"""


class InvalidObjectId(ERSError):
    """对象 ID 不合法（仅允许 1-128 位字母数字、下划线、连字符）。"""


class InvalidRangeHeader(ERSError):
    """Range 请求头语法不被支持。"""


class RangeNotSatisfiable(ERSError):
    """范围合法但无法在当前对象长度上满足（对应 HTTP 416）。"""


class IntegrityError(ERSError):
    """密文、清单或结构未通过认证 / 一致性校验。

    触发场景包括：魔数或版本不符、清单 GCM 标签无效、任一块标签无效、
    AAD 中的长度/位置/对象标识不一致、文件截断、存在未认证尾部字节等。
    """
