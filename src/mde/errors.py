"""mde 的错误类型。所有自定义异常都继承 MdeError。"""


class MdeError(Exception):
    """基础异常。"""


class PolicyValidationError(MdeError):
    """策略定义不合法（未知动作、未知泛化器、别名冲突等）。"""


class PolicyNotFound(MdeError):
    """策略或指定版本不存在。"""


class PolicyConflict(MdeError):
    """策略乐观并发冲突：expected_version 与当前版本不一致。"""

    def __init__(self, policy_id: str, expected: int, current: int):
        self.policy_id = policy_id
        self.expected = expected
        self.current = current
        super().__init__(
            f"policy {policy_id!r} version conflict: "
            f"expected {expected}, current {current}"
        )


class GeneralizerError(MdeError):
    """泛化执行失败（引擎会据此对该字段采取“失败即拒绝”）。"""


class VerificationError(MdeError):
    """导出包核验失败（摘要不符、签名无效、重算结果不一致等）。"""
