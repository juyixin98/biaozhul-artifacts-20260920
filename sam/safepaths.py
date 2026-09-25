"""路径安全: 保证清单中的相对路径无法逃逸制品根目录。

采用 *双重* 防护:

1. 词法检查 (:func:`validate_relative`): 在触碰文件系统之前,
   拒绝任何可疑路径成分 —— 绝对路径、驱动器号、`.`/`..` 段、
   反斜杠 (Windows 下是分隔符, POSIX 下是合法文件名字符, 跨平台
   语义不明确, 一律不允许出现在清单元数据里)、NUL、空段等。
2. 解析后包含检查 (:func:`resolve_within`): 对真实文件系统
   `Path.resolve()` (会展开全部符号链接), 再确认结果仍位于
   已解析的根目录之内 —— 防止 "词法合法但符号链接指向外部"。
"""

from __future__ import annotations

import os
from pathlib import Path

from .errors import UnsafePathError, UnsupportedFileTypeError

# 各平台均保留的非法字符 (NUL) ; 反斜杠按上述说明禁止
_FORBIDDEN_CHARS = ("\x00", "\\")


def validate_relative(rel: str) -> str:
    """词法校验清单相对路径; 通过则原样返回, 否则抛 :class:`UnsafePathError`。

    规则:
      - 必须是非空 str;
      - 必须是 POSIX 风格相对路径, 以 "/" 分隔;
      - 不允许以 "/" 开头 (绝对路径);
      - 不允许 Windows 驱动器号 (如 C:);
      - 不允许 "." / ".." / 空段;
      - 不允许反斜杠与 NUL。
    """
    if not isinstance(rel, str) or not rel:
        raise UnsafePathError("路径为空或不是字符串")
    if "\x00" in rel:
        raise UnsafePathError(f"路径包含 NUL 字符: {rel!r}")
    if "\\" in rel:
        raise UnsafePathError(
            f"路径包含反斜杠 {rel!r}: 跨平台语义歧义, 请使用 POSIX 风格 '/'"
        )
    if rel.startswith("/"):
        raise UnsafePathError(f"路径是绝对路径: {rel!r}")
    # Windows 驱动器号 e.g. "C:foo"
    drive = Path(rel).drive  # POSIX 恒为 ""; 显式再兜一层
    if drive or (len(rel) >= 2 and rel[1] == ":"):
        raise UnsafePathError(f"路径看起来含驱动器号: {rel!r}")

    parts = rel.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise UnsafePathError(
            f"路径包含空段、'.' 或 '..' (目录逃逸): {rel!r}"
        )
    if any(p.endswith(" ") or p.endswith(".") for p in parts):
        # Windows 尾部空格/点会被系统折叠, 跨平台不一致, 拒绝
        raise UnsafePathError(f"路径段不得以空格或 '.' 结尾: {rel!r}")
    return rel


def resolve_within(root: str | os.PathLike[str], rel: str) -> Path:
    """把 rel 安全拼接到 root 并解析, 确认结果在 root 之内。

    返回解析后的绝对路径 (Path)。软链接被完全展开。
    """
    validate_relative(rel)
    root_path = Path(root).resolve(strict=False)
    candidate = (root_path / rel).resolve(strict=False)
    # is_relative_to: Python 3.9+
    if not (candidate == root_path or root_path in candidate.parents):
        raise UnsafePathError(
            f"路径 {rel!r} 解析后 ({candidate}) 逃逸出制品根 ({root_path})"
        )
    return candidate


def iter_artifact_files(root: str | os.PathLike[str]):
    """确定性地遍历制品根内的全部普通文件 (供签名端枚举)。

    - 结果按 POSIX 相对路径排序;
    - 跟随 *文件* 软链接 (验证/签名按链接目标内容哈希),
      但链接目标必须仍在 root 之内, 否则 UnsafePathError;
    - 不跟随 *目录* 软链接 (防止递归环 / 换根), 直接报错;
    - 不接受 FIFO / 字符设备 / 块设备 / 套接字, 直接报错。
    """
    root_path = Path(root).resolve(strict=True)
    if not root_path.is_dir():
        raise UnsafePathError(f"制品根不是目录: {root_path}")

    found: list[str] = []

    def walk(directory: Path) -> None:
        for entry in sorted(os.scandir(directory), key=lambda e: e.name):
            ep = Path(entry.path)
            is_link = entry.is_symlink()
            if is_link:
                # 统一交给 resolve_within 做包含检查 (词法 + 解析双重)
                rel = ep.relative_to(root_path).as_posix()
                resolved = resolve_within(root_path, rel)
                if resolved.is_dir():
                    raise UnsupportedFileTypeError(
                        f"不支持目录软链接: {rel!r}"
                    )
                if not resolved.is_file():
                    raise UnsupportedFileTypeError(
                        f"软链接不指向普通文件: {rel!r}"
                    )
                found.append(rel)
                continue
            if entry.is_dir(follow_symlinks=False):
                walk(ep)
            elif entry.is_file(follow_symlinks=False):
                found.append(ep.relative_to(root_path).as_posix())
            else:
                raise UnsupportedFileTypeError(
                    f"不支持的文件类型 (FIFO/设备/套接字?): "
                    f"{ep.relative_to(root_path).as_posix()}"
                )

    walk(root_path)
    found.sort()
    return found
