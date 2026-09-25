"""校准评估服务层。

把指标计算包装成本地基础设施服务：接收 JSON 风格的请求字典，
返回统一响应信封。不依赖任何 Web 框架，CLI 或未来的 HTTP 适配层
都可直接复用 :meth:`CalibrationService.evaluate`。

请求格式::

    {
      "y_true": [0, 1, ...],
      "proba":  [0.1, 0.9, ...],
      "sample_weight": [1.0, 2.0, ...],   // 可选
      "n_bins": 10,                        // 可选，默认 10
      "endpoint_strategy": "clip",         // 可选: clip | error | ignore
      "epsilon": 1e-15                     // 可选，仅 clip 用
    }

成功响应::

    {"success": true, "data": { ...指标结果... }, "error": null}

失败响应::

    {"success": false, "data": null,
     "error": {"code": "...", "message": "..."}}
"""

from __future__ import annotations

from .errors import CalibrationError
from .metrics import CLIP_EPSILON, ENDPOINT_STRATEGIES, evaluate_calibration

_REQUIRED_FIELDS = ("y_true", "proba")
_OPTIONAL_DEFAULTS = {
    "sample_weight": None,
    "n_bins": 10,
    "endpoint_strategy": "clip",
    "epsilon": CLIP_EPSILON,
}


class CalibrationService:
    """无状态的校准评估服务。"""

    def evaluate(self, request: dict) -> dict:
        """处理一次评估请求，永远返回响应信封、不抛业务异常。"""
        try:
            params = self._parse_request(request)
            result = evaluate_calibration(**params)
            return {"success": True, "data": result, "error": None}
        except CalibrationError as exc:
            return {"success": False, "data": None, "error": exc.to_dict()}

    @staticmethod
    def _parse_request(request: dict) -> dict:
        if not isinstance(request, dict):
            raise CalibrationError(
                "INVALID_REQUEST", "请求体必须是 JSON 对象（字典）"
            )

        for field in _REQUIRED_FIELDS:
            if field not in request:
                raise CalibrationError(
                    "MISSING_FIELD", f"缺少必填字段 {field!r}"
                )

        params = {
            "y_true": request["y_true"],
            "proba": request["proba"],
        }
        for field, default in _OPTIONAL_DEFAULTS.items():
            params[field] = request.get(field, default)

        strategy = params["endpoint_strategy"]
        if strategy not in ENDPOINT_STRATEGIES:
            raise CalibrationError(
                "INVALID_STRATEGY",
                f"endpoint_strategy 必须是 {ENDPOINT_STRATEGIES} 之一，"
                f"收到 {strategy!r}",
            )
        return params
