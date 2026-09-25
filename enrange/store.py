"""Fixed-block AEAD object store.

On-disk format (all multi-byte integers big-endian), one file per object at
``<data_dir>/objects/<object_id>.bin``:

    +--------------------+-----------------------------------------------+
    | header (37 bytes)  | encapsulated block frames                     |
    +--------------------+-----------------------------------------------+

Header::

    magic              8 bytes   b"ENRANGE1"
    version            1 byte    0x01
    block_size         uint32    plaintext bytes per non-final block
    plaintext_len      uint64    total plaintext length of the object
    encap_bytes        uint64    total length of the encapsulated region
    num_blocks         uint32    number of block frames (0 for empty object)
    header_tag         16 bytes  AES-GCM tag over AAD (payload is empty)

Block frame ``i`` is ciphertext||tag with no framing header: its length is
``block_size + 16`` for full blocks and ``remainder + 16`` for the final short
block. GCM nonces are deterministic counters derived from the block index (see
``crypto.py``), so they need not be stored. Every frame is authenticated with
AAD binding the object id, block size, block index, plaintext offset, this
block's length, total object length and total block count.

Verification is all-or-nothing before any plaintext leaves the store: the
header is verified first, then every block touched by a read is decrypted and
AEAD-verified. A tampered byte raises AuthenticationError and the caller gets
no data.
"""

from __future__ import annotations

import os
import re
import secrets
import struct
from pathlib import Path
from typing import Iterator, Optional

from cryptography.exceptions import InvalidTag

from . import crypto
from .errors import (
    AuthenticationError,
    InvalidRangeError,
    NotFoundError,
)

MAGIC = b"ENRANGE1"
VERSION = 1
HEADER_SIZE = 8 + 1 + 4 + 8 + 8 + 4 + crypto.TAG_BYTES  # 49 bytes
DEFAULT_BLOCK_SIZE = 64 * 1024
MAX_BLOCK_SIZE = 2**24 - 1
_ID_RE = re.compile(r"^[0-9a-f]{32}$")
KEY_FILE_NAME = "master.key"
OBJECTS_DIR_NAME = "objects"


def new_object_id() -> str:
    """Return a fresh random 32-hex-char object id."""
    return secrets.token_hex(16)


def _validate_object_id(object_id: str) -> bytes:
    if not isinstance(object_id, str) or not _ID_RE.match(object_id):
        raise ValueError("object_id must be 32 lowercase hex characters")
    return bytes.fromhex(object_id)


def _validate_block_size(block_size: int) -> None:
    if not isinstance(block_size, int) or not 1 <= block_size <= MAX_BLOCK_SIZE:
        raise ValueError(f"block_size must be an int in [1, {MAX_BLOCK_SIZE}]")


class _Header:
    __slots__ = ("block_size", "plaintext_len", "encap_bytes", "num_blocks")

    def __init__(self, block_size: int, plaintext_len: int, encap_bytes: int, num_blocks: int):
        self.block_size = block_size
        self.plaintext_len = plaintext_len
        self.encap_bytes = encap_bytes
        self.num_blocks = num_blocks


def expected_num_blocks(plaintext_len: int, block_size: int) -> int:
    if plaintext_len == 0:
        return 0
    return (plaintext_len - 1) // block_size + 1


def expected_encap_bytes(plaintext_len: int, block_size: int, num_blocks: int) -> int:
    if num_blocks == 0:
        return 0
    full_blocks = num_blocks - 1
    last_len = plaintext_len - full_blocks * block_size
    if not 1 <= last_len <= block_size:
        raise AuthenticationError("header metadata is internally inconsistent")
    return full_blocks * (block_size + crypto.TAG_BYTES) + (last_len + crypto.TAG_BYTES)


class ObjectStore:
    """Directory-backed encrypted object store.

    Parameters
    ----------
    data_dir:
        Directory holding ``master.key`` and ``objects/``.
    master_key:
        32-byte AES-256 master key. Use :meth:`open_or_create` to manage the
        key file on disk (written 0600).
    """

    def __init__(self, data_dir: os.PathLike[str] | str, master_key: bytes):
        if len(master_key) != crypto.MASTER_KEY_BYTES:
            raise ValueError("master key must be exactly 32 bytes")
        self.data_dir = Path(data_dir)
        self.objects_dir = self.data_dir / OBJECTS_DIR_NAME
        self._master_key = master_key

    # ------------------------------------------------------------------ setup

    @classmethod
    def open_or_create(cls, data_dir: os.PathLike[str] | str) -> "ObjectStore":
        """Open a store, creating the directory layout and a local random key.

        The key is generated locally with ``os.urandom`` and stored only in
        ``<data_dir>/master.key`` with mode 0600. No production accounts or KMS
        are involved.
        """
        base = Path(data_dir)
        (base / OBJECTS_DIR_NAME).mkdir(parents=True, exist_ok=True)
        key_path = base / KEY_FILE_NAME
        if key_path.exists():
            key = key_path.read_bytes()
            if len(key) != crypto.MASTER_KEY_BYTES:
                raise ValueError(f"{key_path} is corrupt: expected 32 bytes")
        else:
            key = crypto.generate_master_key()
            # Open with 0600 directly so the key is never world-readable,
            # not even briefly.
            fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                os.write(fd, key)
                os.fsync(fd)
            finally:
                os.close(fd)
        return cls(base, key)

    @property
    def master_key(self) -> bytes:
        """Raw master key (needed by the rotate/inspect tooling only)."""
        return self._master_key

    def _path(self, object_id: str) -> Path:
        raw_id = _validate_object_id(object_id)  # also defeats path traversal
        return self.objects_dir / f"{raw_id.hex()}.bin"

    # ------------------------------------------------------------------ write

    def put(
        self,
        object_id: str,
        data: bytes,
        block_size: int = DEFAULT_BLOCK_SIZE,
    ) -> dict:
        """Encrypt and atomically store ``data`` under ``object_id``."""
        if not isinstance(data, (bytes, bytearray, memoryview)):
            raise TypeError("data must be bytes-like")
        data = bytes(data)
        _validate_block_size(block_size)
        raw_id = _validate_object_id(object_id)
        object_key = crypto.derive_object_key(self._master_key, raw_id)

        plaintext_len = len(data)
        num_blocks = expected_num_blocks(plaintext_len, block_size)
        encap_bytes = expected_encap_bytes(plaintext_len, block_size, num_blocks)

        header_aad = crypto.header_aad(
            raw_id, block_size, plaintext_len, encap_bytes
        )
        header_tag = crypto.seal_header(object_key, crypto.HEADER_PAYLOAD, header_aad)
        if len(header_tag) != crypto.TAG_BYTES:
            # Empty GCM plaintext seals to exactly the 16-byte tag.
            raise RuntimeError("unexpected header ciphertext length")

        header = b"".join(
            (
                MAGIC,
                bytes([VERSION]),
                struct.pack(">I", block_size),
                struct.pack(">Q", plaintext_len),
                struct.pack(">Q", encap_bytes),
                struct.pack(">I", num_blocks),
                header_tag,
            )
        )
        if len(header) != HEADER_SIZE:
            raise RuntimeError("header size mismatch")

        # Build to a temp file in the same directory, fsync, then atomically
        # rename into place.
        tmp_path = self.objects_dir / f".{object_id}.{os.getpid()}.tmp"
        fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(fd, header)
            for index in range(num_blocks):
                offset = index * block_size
                chunk = data[offset : offset + block_size]
                aad = crypto.block_aad(
                    raw_id,
                    block_size,
                    index,
                    offset,
                    len(chunk),
                    plaintext_len,
                    num_blocks,
                )
                frame = crypto.seal_block(object_key, index, chunk, aad)
                os.write(fd, frame)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp_path, self._path(object_id))
        return {
            "id": object_id,
            "length": plaintext_len,
            "block_size": block_size,
            "blocks": num_blocks,
        }

    # ------------------------------------------------------------------ read

    def _load_verified_header(self, raw_id: bytes, path: Path) -> _Header:
        """Read and authenticate an object header. Raises on any problem."""
        try:
            with open(path, "rb") as fh:
                raw_header = fh.read(HEADER_SIZE)
        except FileNotFoundError:
            raise NotFoundError(f"object {raw_id.hex()} not found")
        except OSError as exc:
            raise AuthenticationError(f"cannot read object: {exc}") from exc

        if len(raw_header) != HEADER_SIZE:
            raise AuthenticationError("object header is truncated")
        if raw_header[: len(MAGIC)] != MAGIC or raw_header[8] != VERSION:
            raise AuthenticationError("bad magic/version: object is not enrange v1")

        block_size = struct.unpack(">I", raw_header[9:13])[0]
        plaintext_len = struct.unpack(">Q", raw_header[13:21])[0]
        encap_bytes = struct.unpack(">Q", raw_header[21:29])[0]
        num_blocks = struct.unpack(">I", raw_header[29:33])[0]
        header_tag = raw_header[33:49]

        if block_size < 1:
            raise AuthenticationError("block_size must be >= 1")
        calc_blocks = expected_num_blocks(plaintext_len, block_size)
        if num_blocks != calc_blocks:
            raise AuthenticationError("block count does not match object length")
        try:
            calc_encap = expected_encap_bytes(plaintext_len, block_size, num_blocks)
        except AuthenticationError:
            raise
        if encap_bytes != calc_encap:
            raise AuthenticationError("encapsulated length does not match metadata")

        object_key = crypto.derive_object_key(self._master_key, raw_id)
        aad = crypto.header_aad(raw_id, block_size, plaintext_len, encap_bytes)
        try:
            crypto.open_header(object_key, header_tag, aad)
        except InvalidTag as exc:
            raise AuthenticationError("header authentication failed") from exc

        # Length check: any truncation or appended junk is tampering.
        actual_size = path.stat().st_size
        if actual_size != HEADER_SIZE + encap_bytes:
            raise AuthenticationError("object body length does not match header")

        return _Header(block_size, plaintext_len, encap_bytes, num_blocks)

    def _frame_location(self, hdr: _Header, index: int) -> tuple[int, int]:
        """Return (file offset, frame length) of block frame ``index``."""
        if not 0 <= index < hdr.num_blocks:
            raise AuthenticationError("block index outside header-declared range")
        if index < hdr.num_blocks - 1:
            return HEADER_SIZE + index * (hdr.block_size + crypto.TAG_BYTES), (
                hdr.block_size + crypto.TAG_BYTES
            )
        full_blocks = hdr.num_blocks - 1
        last_plain_len = hdr.plaintext_len - full_blocks * hdr.block_size
        return HEADER_SIZE + full_blocks * (hdr.block_size + crypto.TAG_BYTES), (
            last_plain_len + crypto.TAG_BYTES
        )

    def _iter_verified_blocks(
        self, object_id: str, first_block: int, last_block: int
    ) -> Iterator[tuple[int, bytes]]:
        """Yield ``(block_index, plaintext)`` after each block verifies.

        The header has already authenticated when this runs. A failure on any
        block raises AuthenticationError; blocks already yielded were fully
        verified before being yielded.
        """
        raw_id = _validate_object_id(object_id)
        path = self._path(object_id)
        hdr = self._load_verified_header(raw_id, path)
        object_key = crypto.derive_object_key(self._master_key, raw_id)

        if not 0 <= first_block <= last_block < hdr.num_blocks:
            raise AuthenticationError("block window outside header-declared range")

        with open(path, "rb") as fh:
            for index in range(first_block, last_block + 1):
                offset, frame_len = self._frame_location(hdr, index)
                fh.seek(offset)
                frame = fh.read(frame_len)
                if len(frame) != frame_len:
                    raise AuthenticationError(f"block {index} is truncated")
                plain_offset = index * hdr.block_size
                plain_len = min(
                    hdr.block_size, hdr.plaintext_len - plain_offset
                )
                aad = crypto.block_aad(
                    raw_id,
                    hdr.block_size,
                    index,
                    plain_offset,
                    plain_len,
                    hdr.plaintext_len,
                    hdr.num_blocks,
                )
                try:
                    plain = crypto.open_block(object_key, index, frame, aad)
                except InvalidTag as exc:
                    raise AuthenticationError(
                        f"block {index} authentication failed"
                    ) from exc
                if len(plain) != plain_len:
                    raise AuthenticationError(
                        f"block {index} decrypted length mismatch"
                    )
                yield index, plain

    def read_range(
        self,
        object_id: str,
        start: int = 0,
        end: Optional[int] = None,
    ) -> bytes:
        """Return authenticated plaintext for half-open ``[start, end)``.

        ``end=None`` means "to the end of the object". The header and every
        block overlapping the range are AEAD-verified first; no byte is
        returned from a block that failed verification.
        """
        if not isinstance(start, int) or isinstance(start, bool):
            raise TypeError("start must be an int")
        hdr = self.stat(object_id)  # verifies header, existence
        total = hdr["length"]
        if end is None:
            end = total
        if not isinstance(end, int) or isinstance(end, bool):
            raise TypeError("end must be an int or None")
        if start < 0 or end < 0 or start > end or end > total:
            raise InvalidRangeError(
                f"invalid range [{start}, {end}) for object of length {total}"
            )
        if start == end:
            return b""

        block_size = hdr["block_size"]
        first_block = start // block_size
        last_block = (end - 1) // block_size

        out = bytearray()
        for index, plain in self._iter_verified_blocks(
            object_id, first_block, last_block
        ):
            block_start = index * block_size
            lo = max(start, block_start) - block_start
            hi = min(end, block_start + len(plain)) - block_start
            out.extend(plain[lo:hi])
        return bytes(out)

    def get(self, object_id: str) -> bytes:
        """Return the complete authenticated plaintext of an object."""
        return self.read_range(object_id, 0, None)

    def stat(self, object_id: str) -> dict:
        """Return authenticated metadata: length, block_size, blocks."""
        raw_id = _validate_object_id(object_id)
        path = self._path(object_id)
        if not path.exists():
            raise NotFoundError(f"object {object_id} not found")
        hdr = self._load_verified_header(raw_id, path)
        return {
            "id": object_id,
            "length": hdr.plaintext_len,
            "block_size": hdr.block_size,
            "blocks": hdr.num_blocks,
        }

    def exists(self, object_id: str) -> bool:
        raw_id = _validate_object_id(object_id)
        return self._path(object_id).exists()
