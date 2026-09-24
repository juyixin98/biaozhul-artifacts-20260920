"""pytest 公共夹具：内存中构造 tar 归档（含恶意条目）。"""

from __future__ import annotations

import io
import tarfile

import pytest


def tarinfo(name: str, kind: str = "file", *, size: int = 0, linkname: str = "",
            mode: int = 0o644) -> tarfile.TarInfo:
    mapping = {
        "file": tarfile.REGTYPE,
        "dir": tarfile.DIRTYPE,
        "symlink": tarfile.SYMTYPE,
        "hardlink": tarfile.LNKTYPE,
        "char": tarfile.CHRTYPE,
        "fifo": tarfile.FIFOTYPE,
    }
    m = tarfile.TarInfo(name)
    m.type = mapping[kind]
    m.mode = mode
    m.uid = m.gid = 0
    if kind == "file":
        m.size = size
    if kind in ("symlink", "hardlink"):
        m.linkname = linkname
    return m


class TarBuilder:
    """逐步构造 tar 字节流；文件内容按 size 填零或直接给定。"""

    def __init__(self, mode: str = "w") -> None:
        self._buf = io.BytesIO()
        self._tar = tarfile.open(fileobj=self._buf, mode=mode)
        self._members: list[tarfile.TarInfo] = []

    def add(self, member: tarfile.TarInfo, data: bytes | None = None) -> "TarBuilder":
        if member.isfile():
            if data is None:
                data = b"x" * member.size
            assert len(data) == member.size, "数据长度必须与 size 一致"
            self._tar.addfile(member, io.BytesIO(data))
        else:
            self._tar.addfile(member)
        self._members.append(member)
        return self

    def add_file(self, name: str, data: bytes, mode: int = 0o644) -> "TarBuilder":
        m = tarinfo(name, "file", size=len(data), mode=mode)
        return self.add(m, data)

    def add_dir(self, name: str, mode: int = 0o755) -> "TarBuilder":
        return self.add(tarinfo(name, "dir", mode=mode))

    def add_symlink(self, name: str, target: str, mode: int = 0o777) -> "TarBuilder":
        return self.add(tarinfo(name, "symlink", linkname=target, mode=mode))

    def add_hardlink(self, name: str, target: str) -> "TarBuilder":
        return self.add(tarinfo(name, "hardlink", linkname=target))

    def getvalue(self) -> bytes:
        self._tar.close()
        return self._buf.getvalue()


@pytest.fixture
def builder():
    return TarBuilder


@pytest.fixture
def tarbuilder_factory():
    def _factory(compress: str = "w"):
        return TarBuilder(mode=compress)
    return _factory
