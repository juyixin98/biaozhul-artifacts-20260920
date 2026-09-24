"""规范化 JSON 序列化 (JCS 风格: 键排序、紧凑分隔符、UTF-8)。

内容哈希只依赖数据本身, 与 JSON 里键的书写顺序、空白和换行无关。
"""

from __future__ import annotations

import json
from typing import Any


def canonical_bytes(obj: Any) -> bytes:
    return json.dumps(
        obj,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
