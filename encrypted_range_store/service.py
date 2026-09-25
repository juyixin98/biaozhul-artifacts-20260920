"""本地服务层：对象存储、主密钥保管与 HTTP Range 语义。

纯文件系统后端，不连接任何外部账号或服务。
主密钥为本地随机生成的 32 字节，存于数据目录（权限 0600），仅用于本地测试。
"""

from __future__ import annotations

import os
import re
import stat
from contextlib import contextmanager

from .errors import (
    InvalidObjectId,
    InvalidRangeHeader,
    ObjectNotFound,
    RangeNotSatisfiable,
)
from .format import (
    DEFAULT_BLOCK_SIZE,
    KEY_LEN,
    generate_master_key,
    open_object,
    write_object,
)

_OBJECT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


def validate_object_id(object_id: str) -> str:
    """对象 ID 白名单：字母数字、下划线、连字符，1-128 字符。

    显式拒绝路径分隔符、点、空字节等，杜绝路径穿越。
    """
    if not isinstance(object_id, str) or not _OBJECT_ID_RE.fullmatch(object_id):
        raise InvalidObjectId(
            "object_id 仅允许 1-128 位字母、数字、下划线或连字符"
        )
    return object_id


def parse_range(header: str, length: int) -> tuple[int, int]:
    """解析单个 HTTP byte-range，返回 [start, end_exclusive)。

    支持：bytes=start-end、bytes=start-、bytes=-suffix。
    多区间（逗号）与其它单位 / 语法一律报 InvalidRangeHeader（HTTP 400）。
    区间合法但无法在 length 上满足时报 RangeNotSatisfiable（HTTP 416）。
    """
    if not header.startswith("bytes="):
        raise InvalidRangeHeader("仅支持 bytes 范围单位")
    spec = header[len("bytes="):].strip()
    if "," in spec:
        raise InvalidRangeHeader("不支持多区间范围请求")
    if not spec or "-" not in spec:
        raise InvalidRangeHeader("Range 语法非法")
    start_s, end_s = spec.split("-", 1)
    start_s, end_s = start_s.strip(), end_s.strip()

    if start_s == "":
        # 后缀范围：最后 N 字节
        if not end_s.isdigit():
            raise InvalidRangeHeader("后缀范围必须是非负整数")
        suffix = int(end_s)
        if suffix == 0 or length == 0:
            raise RangeNotSatisfiable("空后缀范围或空对象无法满足")
        start = max(0, length - suffix)
        return start, length

    if not start_s.isdigit():
        raise InvalidRangeHeader("范围起点必须是非负整数")
    start = int(start_s)
    if start >= length:
        raise RangeNotSatisfiable("范围起点超出对象长度")
    if end_s == "":
        return start, length
    if not end_s.isdigit():
        raise InvalidRangeHeader("范围终点必须是非负整数")
    end = int(end_s)
    if end < start:
        raise RangeNotSatisfiable("范围终点小于起点")
    # HTTP 语义：end 为闭区间且允许超过对象末尾（截断到对象末尾）。
    return start, min(end + 1, length)


class KeyFile:
    """本地主密钥保管：缺失时随机生成，文件权限 0600。"""

    def __init__(self, path: str):
        self.path = path
        self.key = self._load_or_create()

    def _load_or_create(self) -> bytes:
        if os.path.exists(self.path):
            with open(self.path, "rb") as f:
                key = f.read()
            if len(key) != KEY_LEN:
                raise ValueError("主密钥文件损坏：长度不是 32 字节")
            mode = stat.S_IMODE(os.stat(self.path).st_mode)
            if mode & 0o077:
                # 不自动改权限，给出明确警告；本地测试场景下这是配置错误。
                raise PermissionError(
                    f"主密钥文件权限过宽（{mode:03o}），应收紧为 0600"
                )
            return key
        key = generate_master_key()
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(fd, key)
        finally:
            os.close(fd)
        return key


class EncryptedObjectStore:
    """加密对象存储：put / get / get_range / delete。"""

    def __init__(
        self,
        data_dir: str,
        block_size: int = DEFAULT_BLOCK_SIZE,
        master_key: bytes | None = None,
    ):
        self.data_dir = data_dir
        self.objects_dir = os.path.join(data_dir, "objects")
        os.makedirs(self.objects_dir, exist_ok=True)
        self.block_size = block_size
        if master_key is None:
            master_key = KeyFile(os.path.join(data_dir, "master.key")).key
        if len(master_key) != KEY_LEN:
            raise ValueError("master_key 必须为 32 字节")
        self._master_key = master_key

    def _path(self, object_id: str) -> str:
        validate_object_id(object_id)
        return os.path.join(self.objects_dir, object_id)

    def put(self, object_id: str, plaintext: bytes) -> None:
        write_object(
            self._path(object_id), object_id, plaintext,
            self._master_key, self.block_size,
        )

    def exists(self, object_id: str) -> bool:
        return os.path.exists(self._path(object_id))

    def size(self, object_id: str) -> int:
        with self._open(object_id) as reader:
            return reader.plaintext_len

    @contextmanager
    def _open(self, object_id: str):
        path = self._path(object_id)
        if not os.path.exists(path):
            raise ObjectNotFound(f"对象不存在：{object_id}")
        reader = open_object(path, object_id, self._master_key)
        try:
            yield reader
        finally:
            reader.close()

    def get(self, object_id: str) -> bytes:
        with self._open(object_id) as reader:
            if reader.plaintext_len == 0:
                return b""
            return reader.read_range(0, reader.plaintext_len)

    def get_range(self, object_id: str, start: int, end_exclusive: int) -> bytes:
        """读取明文 [start, end_exclusive)；范围内每块认证通过后才返回。"""
        with self._open(object_id) as reader:
            return reader.read_range(start, end_exclusive)

    def read_http_range(self, object_id: str, header: str) -> tuple[bytes, int, int, int]:
        """按 HTTP Range 头读取，返回 (body, start, end_exclusive, total_length)。"""
        with self._open(object_id) as reader:
            total = reader.plaintext_len
            start, end_exclusive = parse_range(header, total)
            return reader.read_range(start, end_exclusive), start, end_exclusive, total

    def delete(self, object_id: str) -> None:
        path = self._path(object_id)
        try:
            os.remove(path)
        except FileNotFoundError as exc:
            raise ObjectNotFound(f"对象不存在：{object_id}") from exc
