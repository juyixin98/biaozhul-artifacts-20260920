"""tar 子集的安全预检与隔离解包。

设计要点
========

1. **两阶段处理**：先在纯内存的“虚拟文件系统”上校验全部条目（路径、
   符号链接链、硬链接目标、配额），任何一条不通过都不会触碰磁盘写出。
2. **隔离目录 + 原子发布**：实际解包先写入目标根内的临时暂存目录，
   全部成功后才 ``os.rename`` 为正式目录；任何失败都只删除暂存目录，
   正式目录永远不会以半成品状态出现。
3. **不信任 tar 头声明**：解压不使用 ``TarFile.extract/extractall``，
   只允许常规文件、目录、符号链接、硬链接四种条目；写数据时按
   *实际读到的字节块* 计数，配额在写入过程中持续生效。
4. **纵深防御**：规划阶段做符号链接虚拟解析，写出阶段每条路径再做
   ``realpath`` 包含关系校验，两层都要求路径无法离开目标根。

仅支持 tar 子集：plain tar / gzip / bzip2 / xz（由 tarfile 自动探测）。
"""

from __future__ import annotations

import os
import shutil
import tarfile
import tempfile
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import BinaryIO

from .config import Settings
from .errors import UnsafeArchiveError

# 允许写出的条目类型（其余一律拒绝：设备、FIFO、稀疏文件、PAX/GNU 内部项等）
_ALLOWED_TYPES = frozenset(
    {
        tarfile.REGTYPE,
        tarfile.AREGTYPE,
        tarfile.DIRTYPE,
        tarfile.SYMTYPE,
        tarfile.LNKTYPE,
    }
)

_COPY_BUFSIZE = 1024 * 1024  # 1 MiB


class ArchiveReadError(Exception):
    """归档本身无法解析（损坏、格式不支持、压缩错误等）。"""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


@dataclass(frozen=True)
class EntryRecord:
    """单个归档条目的公开摘要。"""

    name: str
    kind: str  # "file" | "dir" | "symlink" | "hardlink"
    size: int
    target: str | None  # 链接目标（符号链接为原始 linkname，硬链接为规范化目标）


@dataclass
class ExtractResult:
    extraction_id: str
    published_path: str
    archive_sha256: str
    compression: str
    archive_size: int
    declared_uncompressed: int
    bytes_written: int  # 实际写出的字节总和（配额以此为准）
    entry_count: int
    entries: list[EntryRecord] = field(default_factory=list)

    def to_manifest(self) -> dict:
        return {
            "extraction_id": self.extraction_id,
            "published_path": self.published_path,
            "archive_sha256": self.archive_sha256,
            "compression": self.compression,
            "archive_size": self.archive_size,
            "declared_uncompressed": self.declared_uncompressed,
            "bytes_written": self.bytes_written,
            "entry_count": self.entry_count,
            "normalized_permissions": {"file": "0600", "dir": "0700"},
            "entries": [
                {"name": e.name, "kind": e.kind, "size": e.size, "target": e.target}
                for e in self.entries
            ],
        }


# --------------------------------------------------------------------------- #
# 路径处理
# --------------------------------------------------------------------------- #
def _normalize_member_name(raw: str) -> str:
    """把归档内名称规范化为不含前导 ``/``、不含 ``..`` 逃逸的相对 posix 路径。"""
    if "\x00" in raw:
        raise UnsafeArchiveError("nul_in_name", f"名称包含 NUL 字节: {raw!r:.80}")
    if raw.startswith(("/", "\\")):
        raise UnsafeArchiveError("absolute_path", f"拒绝绝对路径条目: {raw!r:.80}")
    # Windows 盘符（如 C:foo）在 POSIX 上虽无害，属于跨平台防御
    if len(raw) >= 2 and raw[1] == ":":
        raise UnsafeArchiveError("absolute_path", f"拒绝带盘符的路径: {raw!r:.80}")

    stack: list[str] = []
    for part in PurePosixPath(raw).parts:
        if part in ("/", ""):
            continue
        if part == ".":
            continue
        if part == "..":
            if not stack:
                raise UnsafeArchiveError(
                    "path_traversal", f"路径经 .. 逃逸出目标根: {raw!r:.80}"
                )
            stack.pop()
            continue
        if any(ord(c) < 32 for c in part):
            raise UnsafeArchiveError(
                "control_char_in_name", f"名称包含控制字符: {raw!r:.80}"
            )
        stack.append(part)

    if not stack:
        raise UnsafeArchiveError("empty_name", "条目名称规范化后为空")
    return "/".join(stack)


def _is_sparse(member: tarfile.TarInfo) -> bool:
    for key in member.pax_headers:
        if "sparse" in key.lower():
            return True
    return False


# --------------------------------------------------------------------------- #
# 虚拟文件系统：在真正写盘之前解析符号链接/硬链接链
# --------------------------------------------------------------------------- #
class _VirtualFS:
    """以归档全部条目构建的内存视图。

    ``symlinks``: 名称 -> 原始 linkname
    ``nodes``   : 名称 -> 类型（file/dir/symlink/hardlink）
    """

    def __init__(self, max_hops: int) -> None:
        self.symlinks: dict[str, str] = {}
        self.nodes: dict[str, str] = {}
        self.max_hops = max_hops

    def resolve(self, path: str, *, allow_absolute: bool = False) -> str:
        """逐组件解析归档相对路径，展开符号链接，返回 root 内的规范化路径。

        - 遇到绝对路径或 ``..`` 越界 -> ``link_escape``
        - 符号链接跳数超限（含自引用环路）-> ``symlink_loop``
        - 路径中段经过一个普通文件 -> ``not_a_directory``
        """
        if path.startswith(("/", "\\")) or (
            len(path) >= 2 and path[1] == ":"
        ):
            raise UnsafeArchiveError(
                "link_escape", f"链接目标为绝对路径: {path!r:.80}"
            )

        work = [p for p in path.split("/") if p not in ("", ".")]
        out: list[str] = []
        hops = 0

        while work:
            comp = work.pop(0)
            if comp == "..":
                if not out:
                    raise UnsafeArchiveError(
                        "link_escape", f"链接目标经 .. 逃逸出目标根: {path!r:.80}"
                    )
                out.pop()
                continue
            out.append(comp)
            prefix = "/".join(out)
            kind = self.nodes.get(prefix)

            if prefix in self.symlinks:
                hops += 1
                if hops > self.max_hops:
                    raise UnsafeArchiveError(
                        "symlink_loop",
                        f"符号链接链超过 {self.max_hops} 跳（疑似环路）: {path!r:.80}",
                    )
                target = self.symlinks[prefix]
                if target.startswith(("/", "\\")) or (
                    len(target) >= 2 and target[1] == ":"
                ):
                    raise UnsafeArchiveError(
                        "link_escape", f"符号链接指向绝对路径: {target!r:.80}"
                    )
                out.pop()  # 摘除链接本身，用目标组件替换后继续处理
                tparts = [p for p in target.split("/") if p not in ("", ".")]
                work = tparts + work
                continue

            if kind in ("file", "hardlink") and work:
                raise UnsafeArchiveError(
                    "not_a_directory",
                    f"路径穿过普通文件 {prefix!r} 继续向下: {path!r:.80}",
                )

        if not out:
            raise UnsafeArchiveError(
                "link_escape", f"链接目标解析后逃逸出目标根: {path!r:.80}"
            )
        return "/".join(out)


# --------------------------------------------------------------------------- #
# 规划
# --------------------------------------------------------------------------- #
@dataclass
class _Planned:
    kind: str
    size: int
    target: str | None  # symlink: 原始 linkname；hardlink: 最终文件的规范化路径
    location: str  # 在目标根内实际占据的落点（符号链接经父目录解析，不展开自身）
    member: tarfile.TarInfo


def plan_archive(tar: tarfile.TarFile, settings: Settings) -> tuple[
    dict[str, _Planned], _VirtualFS, int
]:
    """读取全部条目并完成静态校验，返回 (条目表, 虚拟FS, 声明总字节)。"""
    vfs = _VirtualFS(settings.max_symlink_hops)
    planned: dict[str, _Planned] = {}
    declared_total = 0

    members = tar.getmembers()  # 假脱机文件，顺序读完一次
    if len(members) > settings.max_entries:
        raise UnsafeArchiveError(
            "too_many_entries",
            f"条目数 {len(members)} 超过上限 {settings.max_entries}",
        )

    for member in members:
        # tarfile 内部会消费 GNU longname/longlink；PAX 全局/扩展头若泄漏出来直接拒绝
        if member.type not in _ALLOWED_TYPES:
            raise UnsafeArchiveError(
                "unsupported_entry_type",
                f"不支持的条目类型 {member.type!r}: {member.name!r:.80}",
            )
        if member.isdev():
            raise UnsafeArchiveError(
                "device_entry", f"拒绝设备节点条目: {member.name!r:.80}"
            )
        if _is_sparse(member):
            raise UnsafeArchiveError(
                "sparse_entry", f"拒绝稀疏文件（无法按块可靠计费）: {member.name!r:.80}"
            )
        if member.size < 0:
            raise UnsafeArchiveError(
                "bad_size", f"条目大小为负: {member.name!r:.80}"
            )

        name = _normalize_member_name(member.name)
        if name in planned:
            raise UnsafeArchiveError(
                "duplicate_entry", f"归档内存在重复路径: {name!r}"
            )

        if member.isdir():
            kind, size, target = "dir", 0, None
        elif member.issym():
            kind, size = "symlink", 0
            target = member.linkname
            if target is None or target == "":
                raise UnsafeArchiveError(
                    "bad_link", f"符号链接目标为空: {name!r}"
                )
        elif member.islnk():
            kind, size = "hardlink", 0
            target = member.linkname  # 先存原始值，全部条目就绪后再解析
            if not target:
                raise UnsafeArchiveError("bad_link", f"硬链接目标为空: {name!r}")
        else:
            kind = "file"
            size = member.size
            target = None
            if size > settings.max_file_bytes:
                raise UnsafeArchiveError(
                    "file_too_large",
                    f"条目 {name!r} 大小 {size} 超过单文件上限 "
                    f"{settings.max_file_bytes}",
                )

        declared_total += size
        if declared_total > settings.max_total_bytes:
            raise UnsafeArchiveError(
                "quota_exceeded",
                f"声明的解压总大小 {declared_total} 超过配额 "
                f"{settings.max_total_bytes}（预检阶段拒绝）",
            )

        planned[name] = _Planned(
            kind=kind, size=size, target=target, location="", member=member
        )
        vfs.nodes[name] = kind
        if kind == "symlink":
            vfs.symlinks[name] = target  # type: ignore[index]

    # 第二遍：解析全部链接目标（此时 VFS 已完整，与条目在归档中的顺序无关）
    for name, item in planned.items():
        if item.kind == "symlink":
            # 目标必须留在 root 内；允许悬空（目标尚不存在），不允许逃逸
            vfs.resolve(item.target)  # type: ignore[arg-type]
        elif item.kind == "hardlink":
            chain: list[str] = []
            cur_name = _normalize_member_name(item.target)  # type: ignore[arg-type]
            while True:
                target_item = planned.get(cur_name)
                if target_item is None:
                    raise UnsafeArchiveError(
                        "bad_link", f"硬链接 {name!r} 指向不存在的条目: {cur_name!r}"
                    )
                if target_item.kind == "hardlink":
                    if cur_name in chain:
                        raise UnsafeArchiveError(
                            "symlink_loop", f"硬链接链存在环路: {name!r}"
                        )
                    chain.append(cur_name)
                    cur_name = _normalize_member_name(target_item.target)  # type: ignore[arg-type]
                    continue
                break
            if target_item.kind != "file":
                raise UnsafeArchiveError(
                    "bad_link",
                    f"硬链接 {name!r} 的最终目标不是常规文件: {cur_name!r}",
                )
            # 暂存目标归档名；第三遍算出落点后再改写为 root 内落点
            item.target = cur_name

    # 第三遍：计算每个条目在 root 内的实际落点，并检测落点冲突
    # 注意：符号链接自身不参与展开，只展开它的祖先路径
    locations: dict[str, str] = {}
    for name, item in planned.items():
        if item.kind == "symlink":
            if "/" in name:
                parent, base = name.rsplit("/", 1)
                location = vfs.resolve(parent) + "/" + base
            else:
                location = name
        else:
            location = vfs.resolve(name)
        item.location = location
        if location in locations:
            raise UnsafeArchiveError(
                "duplicate_entry",
                f"条目 {name!r} 与 {locations[location]!r} 经链接解析后落到同一路径 "
                f"{location!r}",
            )
        locations[location] = name

    # 第四遍：硬链接目标改写为最终文件落点（经符号目录解析后仍必须在 root 内）
    for name, item in planned.items():
        if item.kind == "hardlink":
            terminal = item.target  # type: ignore[assignment]
            item.target = planned[terminal].location
            # 落点必须在 root 内（location 已保证，这里再校验目标文件确已存在于规划中）
            if planned[terminal].kind != "file":
                raise UnsafeArchiveError(
                    "bad_link", f"硬链接 {name!r} 最终目标不是常规文件"
                )

    return planned, vfs, declared_total


# --------------------------------------------------------------------------- #
# 磁盘写出
# --------------------------------------------------------------------------- #
def _ensure_within(root: str, target: str) -> str:
    """realpath 校验 target 必须位于 root 之内，返回 target 的 realpath。"""
    root_real = os.path.realpath(root)
    target_real = os.path.realpath(target)
    if target_real != root_real and not target_real.startswith(root_real + os.sep):
        raise UnsafeArchiveError(
            "link_escape",
            "写出路径解析后位于目标根之外（realpath 包含关系校验失败）",
        )
    return target_real


def safe_extract(
    tar_stream: BinaryIO,
    output_root: str,
    settings: Settings,
    *,
    extraction_id: str,
    archive_sha256: str,
    archive_size: int,
    compression: str,
    mode: str = "r:*",
) -> ExtractResult:
    """执行“规划 -> 暂存写出 -> 原子发布”。

    ``tar_stream`` 为已落盘的隔离文件（支持随机访问）。成功返回
    :class:`ExtractResult`；任何安全检查失败都抛出
    :class:`UnsafeArchiveError`，且正式目录不会被创建。
    """
    os.makedirs(output_root, exist_ok=True)

    try:
        tar = tarfile.open(fileobj=tar_stream, mode=mode)
    except tarfile.TarError as exc:
        raise ArchiveReadError(f"无法打开归档: {exc}") from exc

    with tar:
        try:
            planned, vfs, declared_total = plan_archive(tar, settings)
        except tarfile.TarError as exc:
            raise ArchiveReadError(f"归档内容损坏，读取条目失败: {exc}") from exc

        if archive_size > 0:
            ratio = declared_total / archive_size
            if ratio > settings.max_compression_ratio:
                raise UnsafeArchiveError(
                    "compression_bomb",
                    f"压缩比 {ratio:.1f} 超过上限 "
                    f"{settings.max_compression_ratio:g}（疑似解压炸弹）",
                )

        # ---- 所有写操作都在暂存目录内完成 ----
        staging = tempfile.mkdtemp(prefix=".staging-", dir=output_root)
        bytes_written = 0
        records: list[EntryRecord] = []
        try:
            # 1) 先建全部目录（含隐式父目录）。此刻尚无任何符号链接，mkdir 不可能被链接利用
            dirs_to_make: set[str] = set()
            for name, item in planned.items():
                location = item.location
                if item.kind == "dir":
                    dirs_to_make.add(location)
                # 其余条目：只确保其各级“祖先”目录存在，绝不能把条目自身建为目录
                parent = location.rsplit("/", 1)[0] if "/" in location else ""
                if parent:
                    dirs_to_make.add(parent)
                    parts = parent.split("/")
                    for i in range(1, len(parts)):
                        dirs_to_make.add("/".join(parts[:i]))

            for dirname in sorted(dirs_to_make, key=lambda p: p.count("/")):
                path = os.path.join(staging, dirname)
                _ensure_within(staging, path)
                # 显式目录与隐式父目录可能重合
                if not os.path.isdir(path):
                    os.mkdir(path, mode=0o700)

            # 2) 符号链接（按名称排序：父目录链接必须先于其子路径条目建立）
            for name in sorted(n for n, i in planned.items() if i.kind == "symlink"):
                item = planned[name]
                link_path = os.path.join(staging, item.location)
                _ensure_within(staging, os.path.dirname(link_path))
                os.symlink(item.target, link_path, target_is_directory=False)
                # 建立后立即解析：悬空链接同样可被 realpath 规范化
                _ensure_within(staging, link_path)

            # 3) 常规文件：流式写出，按实际字节块计入配额
            for name, item in planned.items():
                if item.kind != "file":
                    continue
                dest = os.path.join(staging, item.location)
                _ensure_within(staging, os.path.dirname(dest))
                written_for_entry = 0
                src = tar.extractfile(item.member)
                if src is None:
                    raise UnsafeArchiveError(
                        "archive_read_error", f"无法读取条目数据: {name!r}"
                    )
                try:
                    with src, open(dest, "wb") as dst:
                        os.fchmod(dst.fileno(), 0o600)
                        while True:
                            chunk = src.read(_COPY_BUFSIZE)
                            if not chunk:
                                break
                            written_for_entry += len(chunk)
                            bytes_written += len(chunk)
                            if written_for_entry > settings.max_file_bytes:
                                raise UnsafeArchiveError(
                                    "quota_exceeded",
                                    f"条目 {name!r} 实际写出 "
                                    f"{written_for_entry} 字节，超过单文件上限",
                                )
                            if bytes_written > settings.max_total_bytes:
                                raise UnsafeArchiveError(
                                    "quota_exceeded",
                                    f"实际写出 {bytes_written} 字节，超过总配额 "
                                    f"{settings.max_total_bytes}（解压炸弹）",
                                )
                            dst.write(chunk)
                except tarfile.TarError as exc:
                    raise ArchiveReadError(f"读取条目 {name!r} 数据失败: {exc}") from exc

                # 写完后再次确认落点（防止经符号链接写到 root 外）
                dest_real = _ensure_within(staging, dest)
                expect_real = os.path.realpath(
                    os.path.join(staging, item.location)
                )
                if dest_real != expect_real:
                    raise UnsafeArchiveError(
                        "link_escape",
                        f"条目 {name!r} 实际落点与规划解析结果不一致",
                    )

            # 4) 硬链接：链接到暂存目录内的已写常规文件
            for name, item in planned.items():
                if item.kind != "hardlink":
                    continue
                target_inside = item.target  # type: ignore[assignment]
                link_path = os.path.join(staging, item.location)
                target_path = os.path.join(staging, target_inside)
                _ensure_within(staging, target_path)
                _ensure_within(staging, os.path.dirname(link_path))
                os.link(target_path, link_path)
                _ensure_within(staging, link_path)

            # 5) 目录权限收口（0700，忽略归档声明的 setuid/setgid/属主）
            for dirpath, dirnames, filenames in os.walk(staging, followlinks=False):
                for d in dirnames:
                    full = os.path.join(dirpath, d)
                    if os.path.islink(full):
                        # 符号链接（os.walk 会把链接目录同时放进 dirnames）
                        _ensure_within(staging, full)
                    else:
                        os.chmod(full, 0o700)
                for f in filenames:
                    full = os.path.join(dirpath, f)
                    if os.path.islink(full):
                        _ensure_within(staging, full)

            # 6) 发布前最终复核：暂存树内任何路径（含解析后的链接落点）都不得越界
            staging_real = os.path.realpath(staging)
            for dirpath, dirnames, filenames in os.walk(staging, followlinks=False):
                for leaf in dirnames + filenames:
                    full = os.path.join(dirpath, leaf)
                    real = os.path.realpath(full)
                    if real != staging_real and not real.startswith(
                        staging_real + os.sep
                    ):
                        raise UnsafeArchiveError(
                            "link_escape",
                            "发布前复核发现条目解析到目标根之外",
                        )

        except BaseException:
            # 任何失败：只清理暂存目录，正式目录从未存在，目标根外更无写入
            shutil.rmtree(staging, ignore_errors=True)
            raise

        # ---- 全部成功：原子改名发布 ----
        final_path = os.path.join(output_root, extraction_id)
        if os.path.lexists(final_path):  # 极端情况下的 ID 冲突，宁失败不覆盖
            shutil.rmtree(staging, ignore_errors=True)
            raise UnsafeArchiveError("internal", "发布目录已存在，拒绝覆盖")
        os.rename(staging, final_path)

    # 归档条目顺序不代表写出顺序，按名称稳定排序后对外展示
    for name in sorted(planned):
        item = planned[name]
        records.append(
            EntryRecord(
                name=name,
                kind=item.kind,
                size=item.size,
                target=item.target,
            )
        )

    return ExtractResult(
        extraction_id=extraction_id,
        published_path=os.path.join(os.path.basename(output_root), extraction_id),
        archive_sha256=archive_sha256,
        compression=compression,
        archive_size=archive_size,
        declared_uncompressed=declared_total,
        bytes_written=bytes_written,
        entry_count=len(planned),
        entries=records,
    )
