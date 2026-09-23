"""自定义异常：模型非法 / 重放校验失败。"""


class ModelError(ValueError):
    """JSON 合约模型不满足 DSL 约束（结构、类型、未知算符等）。"""


class ReplayError(RuntimeError):
    """用具体值重放求解器给出的反例时，发现反例无法自洽复现。"""
