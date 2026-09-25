"""JSON 接口层：字典进 / 字典出，错误结构化，可直接包进任意 Web 框架。

本项目是纯后端，不绑定 HTTP 服务；命令行入口见 :mod:`knapsack.cli`。
如需挂到 Flask/FastAPI 等框架，直接对请求体调用 :func:`solve` 即可：
``response = solve(request_json)``，其中 ``response["ok"]`` 为 False 时
HTTP 层可映射为 400。
"""

from __future__ import annotations

from .solver import solve_knapsack
from .validation import ValidationError, validate_payload


def solve(payload: object) -> dict:
    """求解入口。

    成功返回::

        {"ok": True, "status": "optimal" | "timeout", ...求解字段...}

    失败（输入不合法）返回::

        {"ok": False, "error": {"code": "validation_error",
                                "message": str, "field": str | null}}

    求解器内部不会对合法输入抛错；任何其它异常都属于程序缺陷，由调用方
    自行处理（这里不吞异常）。
    """

    try:
        kwargs = validate_payload(payload)
    except ValidationError as exc:
        return {
            "ok": False,
            "error": {
                "code": "validation_error",
                "message": exc.message,
                "field": exc.field,
            },
        }

    result = solve_knapsack(
        capacity=kwargs["capacity"],
        weights=kwargs["weights"],
        values=kwargs["values"],
        timeout_seconds=kwargs["timeout_seconds"],
    )
    return {"ok": True, **result.to_dict()}
