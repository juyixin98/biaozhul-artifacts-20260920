"""验证过程中可能出现的错误类型。

所有“必须整批拒绝更新”的错误都继承 :class:`UpdateRejected`，
API 层据此统一转成 HTTP 响应；``reason`` 是稳定的机器可读错误码。
"""

from __future__ import annotations


class UpdateRejected(Exception):
    """更新包未通过验证，本地信任状态不得发生任何变化。"""

    reason = "rejected"
    http_status = 400


class MetadataError(UpdateRejected):
    """元数据结构/编码不合法（解析失败、字段缺失、类型错误等）。"""

    reason = "malformed"


class SignatureError(UpdateRejected):
    """签名缺失、签名者不被信任或签名验证失败。"""

    reason = "signature"


class ExpiredMetadataError(UpdateRejected):
    """元数据已过 ``expires`` 时间（冻结攻击的关键防线）。"""

    reason = "expired"


class RollbackError(UpdateRejected):
    """新元数据版本号低于或被要求跳号，属于回滚攻击。"""

    reason = "rollback"


class BindingError(UpdateRejected):
    """跨角色绑定不一致：版本引用、哈希或长度对不上（混搭攻击）。"""

    reason = "binding"


class HashMismatchError(UpdateRejected):
    """目标文件或委派元数据的哈希/长度与声明不符。"""

    reason = "hash"


class NoStateError(UpdateRejected):
    """尚未引导（bootstrap），没有可信根。"""

    reason = "no_state"
    http_status = 409


class StateExistsError(UpdateRejected):
    """已经引导过，拒绝重复引导。"""

    reason = "state_exists"
    http_status = 409


class TargetNotFoundError(UpdateRejected):
    """请求下载的目标不在当前可信 targets 元数据中。"""

    reason = "target_not_found"
    http_status = 404
