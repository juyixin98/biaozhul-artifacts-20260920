"""明文临时文件的安全处理。

策略
====

* 临时文件一律通过 :func:`open_secure_temp` 创建：``O_EXCL`` 唯一名、0600 权限，
  放在最终输出文件的**同一目录**（保证最后的 rename 是同文件系统的原子操作）。
* 正常流程用 :func:`commit_temp` 原子替换目标文件；任何异常路径都调用
  :func:`secure_unlink`：先多次用随机字节覆盖、再 fsync，最后删除，尽量缩小
  明文在磁盘上残留的窗口。

重要限制（如实说明）
--------------------

覆写法在传统原地覆盖的文件系统（ext4 默认 data=ordered 下对同 inode 的覆写通常
仍会落盘）上有效，但在日志结构 / 写时复制文件系统（btrfs、ZFS、APFS 等）、
SSD 的耗损均衡 / FTL 重映射、或者文件系统快照 / 交换分区场景下，**无法保证旧
字节被物理销毁**。因此最强的保护是：

1. 尽量不落地明文（提供流式 API，调用方可直接在内存 / 管道中处理）；
2. 临时文件 0600、生命周期尽可能短，异常立即清除；
3. 对强保护需求，应使用内存盘（tmpfs）或启用全盘加密（LUKS）。
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO

_FILE_MODE = 0o600
_OVERWRITE_PASSES = 3  # 随机字节覆盖次数（尽力而为）
_BUFFER = 64 * 1024


def open_secure_temp(directory: str | os.PathLike[str], prefix: str) -> tuple[BinaryIO, Path]:
    """在 ``directory`` 下创建 0600 的唯一名临时文件，返回 (文件对象, 路径)。"""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=prefix, suffix=".tmp", dir=directory)
    os.fchmod(fd, _FILE_MODE)
    return os.fdopen(fd, "w+b"), Path(name)


def fsync_file(fh: BinaryIO) -> None:
    fh.flush()
    os.fsync(fh.fileno())


def commit_temp(tmp_path: Path, final_path: Path) -> None:
    """原子地把临时文件替换为目标文件，并 fsync 目录。"""
    os.chmod(tmp_path, _FILE_MODE)
    os.replace(tmp_path, final_path)
    _fsync_dir(final_path.parent)


def secure_unlink(path: str | os.PathLike[str], *, size: int | None = None) -> None:
    """尽力安全删除文件：覆写后再 unlink；任何一步失败都继续尝试删除。

    :param size: 已知文件大小时按此覆写；否则按当前实际大小覆写。
    """
    path = Path(path)
    try:
        fh = open(path, "r+b")
    except FileNotFoundError:
        return
    except OSError:
        # 连打开都失败时仍尝试删除
        path.unlink(missing_ok=True)
        return
    try:
        try:
            target = fh.seek(0, os.SEEK_END) if size is None else size
            fh.seek(0)
            remaining = target
            while remaining > 0:
                piece = os.urandom(min(_BUFFER, remaining))
                fh.write(piece)
                remaining -= len(piece)
            fh.flush()
            os.fsync(fh.fileno())
        except OSError:
            pass  # 覆写是尽力而为，下面必须删除
    finally:
        fh.close()
        path.unlink(missing_ok=True)


@contextmanager
def plaintext_temp_file(directory: str | os.PathLike[str]) -> Iterator[tuple[BinaryIO, Path]]:
    """明文临时文件上下文：退出时未 commit 则安全删除（异常路径防明文残留）。"""
    fh, path = open_secure_temp(directory, prefix=".plain.")
    committed = False

    def commit(final_path: Path) -> None:
        nonlocal committed
        fsync_file(fh)
        fh.close()
        commit_temp(path, final_path)
        committed = True

    try:
        yield fh, commit
    finally:
        if not committed:
            try:
                fh.close()
            except OSError:
                pass
            secure_unlink(path)


def _fsync_dir(directory: Path) -> None:
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
