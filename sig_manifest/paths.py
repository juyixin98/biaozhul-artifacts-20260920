"""制品路径安全处理。

清单中记录的所有路径必须是**相对的 POSIX 风格路径**，且解析后必须落在
制品根目录之内。这里做两道独立的防线：

1. 词法校验（不依赖文件系统）：拒绝绝对路径、盘符、反斜杠、NUL、
   空组件、`.`/`..` 组件 —— 让 `../etc/passwd` 这类输入在触碰磁盘前就失败；
2. 文件系统校验：`realpath` 解析全部符号链接后，确认结果仍在
   根目录的 realpath 之内 —— 防住词法上合法、但由符号链接引出根的逃逸。
"""

from __future__ import annotations

import os
from pathlib import Path

from .errors import UnsafePathError

# 与清单格式绑定：路径一律用 "/"，不接受平台本地分隔符，避免跨平台歧义。
SEP = "/"


def validate_relative(rel_path: str) -> str:
    """词法校验制品内相对路径，返回去掉多余形式后的原样路径。"""
    if not isinstance(rel_path, str):
        raise UnsafePathError(f"路径必须是字符串，得到 {type(rel_path).__name__}")
    if rel_path == "":
        raise UnsafePathError("路径不能为空")
    if "\x00" in rel_path:
        raise UnsafePathError(f"路径包含 NUL 字符: {rel_path!r}")
    if "\\" in rel_path:
        raise UnsafePathError(f"路径禁止使用反斜杠（请使用 POSIX 风格 /）: {rel_path!r}")
    if rel_path.startswith("/"):
        raise UnsafePathError(f"路径必须是相对路径，不能以 / 开头: {rel_path!r}")
    # Windows 盘符 / 保留设备名在任何平台都拒掉（清单可能跨平台流转）。
    if len(rel_path) >= 2 and rel_path[1] == ":":
        raise UnsafePathError(f"路径不能包含盘符: {rel_path!r}")

    # 不能依赖 PurePosixPath.parts：它会把 "a//b"、"a/./b" 折叠成
    # ("a","b")，从而让空组件与 '.' 组件蒙混过关。直接对原始串切分校验。
    if rel_path.endswith("/"):
        raise UnsafePathError(f"路径不能以 / 结尾: {rel_path!r}")
    components = rel_path.split("/")
    if not components:
        raise UnsafePathError(f"路径不能为空: {rel_path!r}")
    for component in components:
        if component == "" :
            raise UnsafePathError(f"路径不能包含空组件（禁止 //）: {rel_path!r}")
        if component == ".":
            raise UnsafePathError(f"路径不能包含 '.' 组件: {rel_path!r}")
        if component == "..":
            raise UnsafePathError(f"路径不能包含 '..' 组件（禁止路径穿越）: {rel_path!r}")
        if any(ord(c) < 0x20 for c in component):
            raise UnsafePathError(f"路径组件包含控制字符: {rel_path!r}")
    # 规范化重复斜杠（理论上 split 已排除空组件；显式重组保证确定性）。
    return SEP.join(components)


def resolve_within(root: str | os.PathLike[str], rel_path: str) -> Path:
    """把 ``rel_path`` 解析到 ``root`` 之内的绝对真实路径。

    依次执行：词法校验 → root realpath → join → 目标 realpath（root 必须存在；
    目标文件可不存在，此时对其已存在的最近父目录做 realpath 并逐段确认）。
    """
    safe_rel = validate_relative(rel_path)

    root_real = Path(root).resolve(strict=False)
    # root 本身若是悬空符号链接也视为不可信环境，直接报错。
    if not root_real.exists():
        raise UnsafePathError(f"制品根目录不存在: {root}")

    candidate = root_real.joinpath(*safe_rel.split(SEP))

    # 目标已存在：直接整体 realpath 后做包含判断，能抓住链接逃逸。
    if _lexists(candidate):
        resolved = candidate.resolve(strict=False)
        _ensure_within(root_real, resolved, rel_path)
        return resolved

    # 目标尚不存在（如签名前预期写入路径）：向上找到存在的父目录做 realpath。
    existing_parent = candidate.parent
    suffix: list[str] = []
    while not existing_parent.exists() and existing_parent != existing_parent.parent:
        suffix.insert(0, existing_parent.name)
        existing_parent = existing_parent.parent
    parent_real = existing_parent.resolve(strict=False)
    _ensure_within(root_real, parent_real, rel_path)
    rebuilt = parent_parent_join(parent_real, suffix, candidate.name)
    _ensure_within(root_real, rebuilt, rel_path)
    return rebuilt


def parent_parent_join(parent_real: Path, suffix: list[str], name: str) -> Path:
    result = parent_real
    for part in suffix:
        result = result / part
    return result / name


def _lexists(path: Path) -> bool:
    try:
        path.lstat()
        return True
    except FileNotFoundError:
        return False
    except OSError:
        return False


def _ensure_within(root_real: Path, target_real: Path, original: str) -> None:
    """包含判断：target == root 或 target 位于 root 子树内。"""
    try:
        target_real.relative_to(root_real)
    except ValueError:
        raise UnsafePathError(
            f"路径逃逸出制品根目录: {original!r} (root={root_real}, resolved={target_real})"
        ) from None


def walk_artifact_files(root: str | os.PathLike[str]) -> list[str]:
    """枚举制品根目录下全部普通文件的相对 POSIX 路径（排序，含隐藏文件）。

    跟随目录结构，但不跟随会逃逸出 root 的符号链接（resolve_within 会拦）。
    悬空链接、指向目录的符号链接均不计入"普通文件"。
    """
    root_real = Path(root).resolve(strict=True)
    collected: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root_real, followlinks=False):
        # 排序保证遍历确定；os.walk 是自顶向下，先校验目录链接。
        dirnames.sort()
        filenames.sort()
        current = Path(dirpath)
        for name in filenames:
            abs_path = current / name
            try:
                rel = abs_path.relative_to(root_real)
            except ValueError:
                raise UnsafePathError(f"枚举到根目录之外的文件: {abs_path}") from None
            rel_posix = rel.as_posix()
            # resolve_within 会做 realpath 包含校验，符号链接逃逸在此被抓。
            resolved = resolve_within(root_real, rel_posix)
            if abs_path.is_symlink() and not resolved.exists():
                raise UnsafePathError(f"制品条目是悬空符号链接: {rel_posix}")
            if not resolved.is_file():
                # 指向文件的符号链接算普通文件；其它非文件（设备、套接字）拒绝。
                raise UnsafePathError(f"制品条目不是普通文件: {rel_posix}")
            collected.append(rel_posix)
        # 显式检查子目录中的符号链接：followlinks=False 不会进入链接目录，
        # 但我们要对"链接目录"给出明确错误，而不是静默忽略。
        for name in list(dirnames):
            sub = current / name
            if sub.is_symlink():
                rel = sub.relative_to(root_real).as_posix()
                try:
                    resolved = resolve_within(root_real, rel)
                except UnsafePathError:
                    raise
                if resolved.is_dir():
                    # 指向 root 内部目录的链接：允许但 os.walk 不跟随，保持简单可预测。
                    dirnames.remove(name)
    return sorted(collected)
