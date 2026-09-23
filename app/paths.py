"""路径规范化与安全校验。

关键原则：规范化（normalize）绝不能掩盖语义变化。任何有歧义的路径
（盘符、反斜杠、`\\0`、绝对路径、`.`/`..`、空段、符号链接等）一律拒绝，
而不是"清理后继续"。
"""

from __future__ import annotations

import re

_DRIVE_RE = re.compile(r"^[A-Za-z]:")


class UnsafePath(ValueError):
    """路径不满足严格安全规范；拒绝处理。"""


def canonical_source_path(path: str) -> str:
    """把归档条目/内联源码路径严格规范化为正斜杠相对路径。

    拒绝：
      - 非字符串、空串、NUL 及控制字符
      - Windows 盘符 (C:) 与反斜杠（与正斜杠语义冲突，防止跨平台歧义）
      - 前导 `/`（绝对路径）
      - 空段（重复斜杠）、`.`、`..`
      - 首尾空白伪装

    规范化仅做：去首尾空白、按 `/` 切分、拒绝后重组。不做大小写折叠，
    因为不同大小写在真实文件系统中可能指向不同文件。
    """
    if not isinstance(path, str):
        raise UnsafePath("路径必须是字符串")
    stripped = path.strip()
    if not stripped:
        raise UnsafePath("路径为空")
    if "\x00" in stripped or any(ord(c) < 32 for c in stripped):
        raise UnsafePath(f"路径包含控制字符: {path!r}")
    if "\\" in stripped:
        raise UnsafePath(f"路径不允许反斜杠（语义歧义）: {path!r}")
    if stripped.startswith("/"):
        raise UnsafePath(f"不允许绝对路径: {path!r}")
    if _DRIVE_RE.match(stripped):
        raise UnsafePath(f"不允许盘符路径: {path!r}")
    parts = stripped.split("/")
    clean: list[str] = []
    for part in parts:
        if part == "":
            raise UnsafePath(f"路径包含空段（重复斜杠或结尾斜杠）: {path!r}")
        if part in (".", ".."):
            raise UnsafePath(f"路径包含 . 或 .. 段: {path!r}")
        clean.append(part)
    result = "/".join(clean)
    if not result:
        raise UnsafePath(f"路径规范化后为空: {path!r}")
    return result
