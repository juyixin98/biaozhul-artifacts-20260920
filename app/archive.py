"""源码包解包（zip / tar / tar.gz），严格防御 zip-slip、符号链接与压缩炸弹。

**绝不执行包内任何脚本/可执行内容**；这里只做字节提取。
"""

from __future__ import annotations

import io
import struct
import tarfile
import zipfile

from .paths import UnsafePath, canonical_source_path

#: 解包防护上限
MAX_ENTRIES = 2048
MAX_TOTAL_BYTES = 5 * 1024 * 1024  # 全部条目解压后 5 MiB
MAX_NAME_BYTES = 1024

#: tar 中允许的条目类型：普通文件（含 \0）与目录；其余全部拒绝
_TAR_ALLOWED_TYPES = {tarfile.REGTYPE, tarfile.DIRTYPE}


class ArchiveError(ValueError):
    """归档非法或超出安全限制。"""


def _safe_canonical(name: str) -> str:
    try:
        return canonical_source_path(name)
    except UnsafePath as exc:
        raise ArchiveError(str(exc)) from exc


def _put(mapping: dict[str, bytes], name: str, data: bytes) -> None:
    if len(name.encode("utf-8")) > MAX_NAME_BYTES:
        raise ArchiveError(f"路径名过长: {name!r}")
    path = _safe_canonical(name)
    if path in mapping:
        raise ArchiveError(f"归档内重复条目（规范化后冲突）: {path}")
    mapping[path] = data


def _scan_zip_central_directory_raw(buf: bytes) -> list[str]:
    """直接扫描 ZIP 中央目录的**原始**文件名字节，返回解码后的名字列表。

    Python zipfile 在 UTF-8 解码时会截断 NUL，导致 ``"A\\x00.sol"`` 这类
    名字被悄悄改写为 ``"A"``；必须在原始字节层先拒绝。
    """
    try:
        eocd = buf.rindex(b"PK\x05\x06")
    except ValueError:
        raise ArchiveError("zip 缺少中央目录结束记录(EOCD)")
    if len(buf) - eocd < 22:
        raise ArchiveError("zip EOCD 记录不完整")
    total = struct.unpack_from("<H", buf, eocd + 10)[0]
    cd_size = struct.unpack_from("<I", buf, eocd + 12)[0]
    cd_off = struct.unpack_from("<I", buf, eocd + 16)[0]
    if cd_off + cd_size > len(buf):
        raise ArchiveError("zip 中央目录偏移越界")
    p = cd_off
    names: list[str] = []
    for _ in range(total):
        if buf[p : p + 4] != b"PK\x01\x02":
            raise ArchiveError("zip 中央目录条目签名非法")
        flag = struct.unpack_from("<H", buf, p + 8)[0]
        name_len = struct.unpack_from("<H", buf, p + 28)[0]
        extra_len = struct.unpack_from("<H", buf, p + 30)[0]
        comment_len = struct.unpack_from("<H", buf, p + 32)[0]
        raw_name = buf[p + 46 : p + 46 + name_len]
        if b"\x00" in raw_name or any(c < 0x20 for c in raw_name):
            raise ArchiveError(f"zip 条目名包含 NUL/控制字符（原始字节）: {raw_name!r}")
        encoding = "utf-8" if flag & 0x800 else "cp437"
        try:
            names.append(raw_name.decode(encoding))
        except UnicodeDecodeError as exc:
            raise ArchiveError(f"zip 条目名无法按 {encoding} 解码: {exc}") from exc
        p += 46 + name_len + extra_len + comment_len
    return names


def _unzip(buf: bytes) -> dict[str, bytes]:
    out: dict[str, bytes] = {}
    total = 0
    raw_names = _scan_zip_central_directory_raw(buf)
    try:
        zf = zipfile.ZipFile(io.BytesIO(buf))
    except zipfile.BadZipFile as exc:
        raise ArchiveError(f"非法 zip 归档: {exc}") from exc
    infos = zf.infolist()
    if len(infos) > MAX_ENTRIES:
        raise ArchiveError(f"归档条目数 {len(infos)} 超过上限 {MAX_ENTRIES}")
    if len(raw_names) != len(infos):
        raise ArchiveError("zip 中央目录条目数与文件头不一致")
    for info in infos:
        # 仅当 S_IFMT 明确声明为"既非普通文件也非目录"的类型（符号链接 0o120000、
        # 字符/块设备、FIFO、socket 等）时拒绝。权限位（如 0o600）或无 Unix 模式（0）
        # 都按普通文件处理——Python zipfile.writestr 写出的正是这种模式。
        mode = info.external_attr >> 16
        if mode:
            import stat

            fmt = stat.S_IFMT(mode)
            if fmt not in (0, stat.S_IFREG, stat.S_IFDIR):
                raise ArchiveError(f"zip 内含不允许的条目类型: {info.filename!r} (mode={oct(mode)})")
        if info.is_dir():
            _safe_canonical(info.filename)  # 目录名同样必须通过严格路径规范
            continue
        # 先严格校验条目名（NUL、反斜杠、穿越等），再读数据
        canon = _safe_canonical(info.filename)
        if canon in out:
            raise ArchiveError(f"归档内重复条目（规范化后冲突）: {canon}")
        with zf.open(info) as fh:
            data = fh.read()
        total += len(data)
        if total > MAX_TOTAL_BYTES:
            raise ArchiveError(f"解压总大小超过上限 {MAX_TOTAL_BYTES} 字节")
        out[canon] = data
    return out


def stat_is_symlink(mode: int) -> bool:
    import stat

    return stat.S_ISLNK(mode)


def stat_is_reg_or_dir(mode: int) -> bool:
    import stat

    return stat.S_ISREG(mode) or stat.S_ISDIR(mode)


def _untar(buf: bytes, compressed: bool) -> dict[str, bytes]:
    out: dict[str, bytes] = {}
    total = 0
    mode = "r:gz" if compressed else "r:"
    try:
        tf = tarfile.open(fileobj=io.BytesIO(buf), mode=mode)
    except tarfile.TarError as exc:
        raise ArchiveError(f"非法 tar 归档: {exc}") from exc
    count = 0
    for member in tf:
        count += 1
        if count > MAX_ENTRIES:
            raise ArchiveError(f"归档条目数超过上限 {MAX_ENTRIES}")
        if member.type not in _TAR_ALLOWED_TYPES:
            kind = "symlink" if member.issym() else ("hardlink" if member.islnk() else f"type={member.type!r}")
            raise ArchiveError(f"tar 内含不允许的条目类型（{kind}）: {member.name!r}")
        if member.isdir():
            # 目录名本身不存入映射，但仍需通过路径规范校验
            _safe_canonical(member.name)
            continue
        if member.size > MAX_TOTAL_BYTES:
            raise ArchiveError(f"单个条目过大: {member.name!r} ({member.size} 字节)")
        fh = tf.extractfile(member)
        if fh is None:
            raise ArchiveError(f"无法读取 tar 条目: {member.name!r}")
        data = fh.read()
        total += len(data)
        if total > MAX_TOTAL_BYTES:
            raise ArchiveError(f"解压总大小超过上限 {MAX_TOTAL_BYTES} 字节")
        _put(out, member.name, data)
    return out


def extract_archive(buf: bytes, filename: str = "package.zip") -> dict[str, bytes]:
    """提取归档为 ``{规范化路径: 文件字节}``。

    自动识别 zip 与 tar/tar.gz；任何越权条目、路径违规、重复条目、超限内容
    都抛 :class:`ArchiveError`（内部的 :class:`~app.paths.UnsafePath` 会被包装）。
    """
    head = buf[:4]
    name = filename.lower()
    if head[:2] == b"PK":
        return _unzip(buf)
    if head[:2] == b"\x1f\x8b":  # gzip 魔数
        return _untar(buf, compressed=True)
    if name.endswith((".tar.gz", ".tgz")):
        # 声称 gzip 但魔数不符
        raise ArchiveError("文件名指示 tar.gz，但缺少 gzip 魔数")
    # 裸 tar 无可靠魔数，交给 tarfile 尝试
    return _untar(buf, compressed=False)
