"""JSON 接口层：请求解析、响应序列化、stdin/stdout CLI。

用法：
    python -m adaptive_integration.cli < request.json
    python -m adaptive_integration.cli -f request.json

退出码：
    0  请求有效且积分判敛
    1  请求有效但积分失败（奇点/不收敛/预算耗尽等）
    2  请求本身无法解析（非法 JSON / 缺少字段 / 参数越界等）
"""

from __future__ import annotations

import json
from typing import Any

from .api import integrate_request
from .result import IntegrationResult


def parse_request(raw: str) -> dict[str, Any]:
    """解析 JSON 文本；格式错误时抛出 json.JSONDecodeError（由 CLI 捕获）。"""

    return json.loads(raw)


def request_to_json(request: dict[str, Any], *, indent: int = 2) -> str:
    return json.dumps(request, ensure_ascii=False, indent=indent)


def result_to_dict(result: IntegrationResult) -> dict[str, Any]:
    return result.to_dict()


def result_to_json(result: IntegrationResult, *, indent: int = 2) -> str:
    return json.dumps(result.to_dict(), ensure_ascii=False, indent=indent)
