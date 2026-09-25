"""失败状态对应的异常类型。

- KalmanInputError: 输入非法（维度不匹配、非对称/非半正定噪声矩阵、非有限值、
  超出规模上限等）。JSON 接口将其映射为 status="error"。
- KalmanNumericalError: 运行期数值失败（协方差失去半正定性且超出容差、
  出现非有限值等）。JSON 接口同样映射为 status="error"。
"""


class KalmanError(Exception):
    """本库所有异常的基类。"""

    def __init__(self, message, code="kalman_error"):
        super().__init__(message)
        self.code = code


class KalmanInputError(KalmanError):
    """输入校验失败。"""

    def __init__(self, message, code="invalid_input"):
        super().__init__(message, code)


class KalmanNumericalError(KalmanError):
    """运行期数值失败。"""

    def __init__(self, message, code="numerical_failure"):
        super().__init__(message, code)
