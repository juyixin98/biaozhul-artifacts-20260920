"""信封加密核心服务：分块 AEAD 加解密 + 主密钥轮换。

存储布局（默认 0700 目录权限）::

    <store>/
      keys/keystore.json          主密钥库（见 keystore.py）
      meta/<fid>.meta             信封元数据（DEK 信封 + 受保护头）
      blobs/<fid>.blob            分块 AEAD 密文
      .*.tmp                      原子写入临时文件（启动时自动清扫）

密钥关系
========

    明文文件
      │  随机生成数据密钥 DEK（每文件一把，AES-256）
      ▼  分块 AES-256-GCM，AAD = 文件ID + 块序号 + 分块参数，nonce = 块序号派生
    blobs/<fid>.blob
      │  DEK 由主密钥 MK 用计数器 nonce 包裹（envelope）
      ▼  与受保护头一起存入 meta/<fid>.meta

轮换主密钥
----------
用旧 MK 解开 DEK 信封得到 DEK（**只用它重新封装，不落盘**），再用新 MK 重新
包裹 DEK、用 DEK 重新封装受保护头，原子替换 meta 文件。blob 密文一个字节都
不重新加密；这正是信封加密的核心收益。

并发
----
所有操作经单把进程内锁串行化，密钥库的计数器更新另有 flock 与原子落盘；
本服务定位为单机本地工具，不做跨主机分布式协调。
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Callable

from . import blob, crypto, meta as meta_mod
from .crypto import generate_key
from .errors import (
    AEADAuthenticationError,
    CorruptContainerError,
    InvalidFormatError,
    KeyNotFoundError,
    NoMasterKeyError,
    RotationError,
)
from .keystore import KeyStore, StoredMasterKey
from .securetemp import (
    commit_temp,
    fsync_file,
    open_secure_temp,
    plaintext_temp_file,
    secure_unlink,
)

_FILE_ID_BYTES = 16  # 128 位
_TMP_PREFIXES = (".plain.", ".blob.", ".meta.")


def _new_file_id() -> str:
    return os.urandom(_FILE_ID_BYTES).hex()


def _valid_file_id(file_id: str) -> bool:
    return (
        isinstance(file_id, str)
        and len(file_id) == _FILE_ID_BYTES * 2
        and all(c in "0123456789abcdef" for c in file_id)
    )


@dataclass(frozen=True)
class EncryptedLocation:
    """一次加密的产出位置。"""

    file_id: str
    meta_path: Path
    blob_path: Path


@dataclass(frozen=True)
class RotationInfo:
    """一次轮换的结果信息。"""

    file_id: str
    old_kid: str
    new_kid: str
    header_version: int
    blob_bytes_changed: int  # 恒为 0：轮换不动数据块


class EnvelopeService:
    """信封加密服务。"""

    def __init__(self, store_dir: str | os.PathLike[str]):
        self.root = Path(store_dir)
        self.keys_dir = self.root / "keys"
        self.meta_dir = self.root / "meta"
        self.blob_dir = self.root / "blobs"
        for d in (self.root, self.keys_dir, self.meta_dir, self.blob_dir):
            d.mkdir(parents=True, exist_ok=True)
            os.chmod(d, 0o700)
        self.key_store = KeyStore(self.keys_dir)
        self._lock = threading.RLock()
        # 测试用故障注入钩子，签名：fn(stage: str, file_id: str) -> None
        # 已知阶段："rotate-before-replace"（新 meta 已就绪、原子替换前）。
        self.crash_hook: Callable[[str, str], None] | None = None
        self.sweep_temp_files()

    # ------------------------------------------------------------------ 工具

    def sweep_temp_files(self) -> int:
        """清理上次运行可能残留的临时 / 半成品文件（崩溃恢复），返回删除数量。

        清理两类：
        1. ``.blob.*/.meta.*/.plain.*.tmp`` 等临时文件；
        2. **孤儿 blob**：blob 已落盘但 meta 未提交（恰好在两步之间被杀）。
           没有 meta 的 blob 在密码学上不可解读（DEK 只存在于未提交的 meta 中），
           不可能属于任何可访问文件，启动时（无并发写）安全删除。
        """
        removed = 0
        for directory in (self.meta_dir, self.blob_dir):
            for entry in directory.iterdir():
                if entry.is_file() and entry.name.startswith(_TMP_PREFIXES):
                    secure_unlink(entry)
                    removed += 1
        for entry in self.blob_dir.iterdir():
            if entry.is_file() and entry.suffix == ".blob":
                fid = entry.stem
                if _valid_file_id(fid) and not (self.meta_dir / f"{fid}.meta").exists():
                    secure_unlink(entry)
                    removed += 1
        return removed

    def _paths(self, file_id: str) -> tuple[Path, Path]:
        if not _valid_file_id(file_id):
            raise InvalidFormatError("file_id 必须为 32 位小写十六进制字符串")
        return self.meta_dir / f"{file_id}.meta", self.blob_dir / f"{file_id}.blob"

    # ------------------------------------------------------------ 主密钥管理

    def create_master_key(self) -> StoredMasterKey:
        with self._lock:
            return self.key_store.generate_master_key()

    def list_master_keys(self) -> list[StoredMasterKey]:
        with self._lock:
            return self.key_store.list_keys()

    def _latest_key(self) -> StoredMasterKey:
        try:
            kid = self.key_store.latest_kid()
        except KeyNotFoundError as exc:
            raise NoMasterKeyError("密钥库为空：请先 create_master_key()") from exc
        return self.key_store.get(kid)

    # -------------------------------------------------------------- 加密写入

    def encrypt_stream(
        self,
        source: BinaryIO,
        *,
        file_id: str | None = None,
        chunk_size: int = 64 * 1024,
    ) -> EncryptedLocation:
        """对流式明文做分块信封加密。

        流程：生成随机 DEK → 分块 AEAD 写入临时 blob（先 fsync/rename 落盘）
        → 分配持久化包裹 nonce 计数器 → 用最新主密钥包裹 DEK 并密封受保护头
        → 原子写入 meta。任意步骤异常都会清理临时文件，不留明文 / 半成品。
        """
        with self._lock:
            if chunk_size <= 0 or chunk_size > 16 * 1024 * 1024:
                raise ValueError("chunk_size 必须在 1 ~ 16 MiB 之间")
            mk = self._latest_key()
            fid = file_id or _new_file_id()
            if not _valid_file_id(fid):
                raise InvalidFormatError("file_id 必须为 32 位小写十六进制字符串")
            meta_path, blob_path = self._paths(fid)
            if meta_path.exists() or blob_path.exists():
                raise FileExistsError(f"文件已存在，拒绝覆盖：{fid}")

            dek = generate_key()
            source.seek(0, os.SEEK_END)
            plaintext_size = source.tell()
            source.seek(0)
            expected_blocks = blob.expected_blocks_for_size(plaintext_size, chunk_size)

            blob_fh, tmp_blob = open_secure_temp(self.blob_dir, prefix=".blob.")
            tmp_meta: Path | None = None
            try:
                blocks = blob.stream_encrypt(
                    source,
                    blob_fh,
                    dek=dek,
                    file_id=fid,
                    chunk_size=chunk_size,
                )
                if blocks != expected_blocks:  # 理论不可达，防御性检查
                    raise CorruptContainerError("加密块数与明文长度不一致")
                fsync_file(blob_fh)
                blob_fh.close()
                commit_temp(tmp_blob, blob_path)  # 崩溃点之后只会留下孤儿 blob

                counter = self.key_store.allocate_wrap_counter()
                meta_bytes = meta_mod.seal_meta(
                    file_id=fid,
                    kid=mk.kid,
                    dek=dek,
                    master_key=mk.key_material,
                    plaintext_size=plaintext_size,
                    chunk_size=chunk_size,
                    blocks=blocks,
                    header_version=1,
                    wrap_counter=counter,
                )

                meta_fh, tmp_meta = open_secure_temp(self.meta_dir, prefix=".meta.")
                meta_fh.write(meta_bytes)
                fsync_file(meta_fh)
                meta_fh.close()
                commit_temp(tmp_meta, meta_path)
                return EncryptedLocation(fid, meta_path, blob_path)
            except BaseException:
                try:
                    blob_fh.close()
                except OSError:
                    pass
                if tmp_blob.exists():
                    secure_unlink(tmp_blob)
                if blob_path.exists() and not meta_path.exists():
                    # meta 尚未提交：把刚提交的孤儿 blob 一并清掉
                    secure_unlink(blob_path)
                if tmp_meta is not None:
                    secure_unlink(tmp_meta)
                raise

    def encrypt_file(
        self,
        plaintext_path: str | os.PathLike[str],
        *,
        chunk_size: int = 64 * 1024,
    ) -> EncryptedLocation:
        """加密磁盘上的明文文件。本方法不删除源文件——是否删除、如何安全删除
        由调用方按其威胁模型决定（可用 :func:`securetemp.secure_unlink`）。"""
        with open(plaintext_path, "rb") as src:
            return self.encrypt_stream(src, chunk_size=chunk_size)

    # -------------------------------------------------------------- 解密读取

    def _open_envelope(
        self, file_id: str
    ) -> tuple[dict, dict, bytes, StoredMasterKey]:
        """读取并解开信封，返回 (外层信息, 受保护头, DEK, 主密钥条目)。

        解密顺序本身即认证顺序：主密钥错误 / 信封被篡改 → 第 1 次 AEAD 失败；
        受保护头被篡改 → 第 2 次 AEAD 失败；头内字段不一致 → 结构校验失败。
        """
        meta_path, _ = self._paths(file_id)
        if not meta_path.exists():
            raise FileNotFoundError(f"信封元数据不存在：{file_id}")
        outer = meta_mod.read_meta_bytes(meta_path)
        if outer["fid"] != file_id:
            raise AEADAuthenticationError("meta 外层文件 ID 与请求的文件 ID 不一致")

        try:
            mk = self.key_store.get(outer["kid"])
        except KeyNotFoundError:
            raise KeyNotFoundError(
                f"该文件由主密钥 {outer['kid']} 包裹，但此密钥不在密钥库中"
            ) from None

        dek = crypto.aead_open(
            mk.key_material,
            outer["envelope_nonce"],
            outer["envelope_ct"],
            crypto.wrap_aad(file_id=outer["fid"]),
        )
        header_raw = crypto.aead_open(
            dek,
            outer["header_nonce"],
            outer["header_ct"],
            crypto.header_aad(file_id=outer["fid"]),
        )
        import json

        try:
            header = json.loads(header_raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise InvalidFormatError("受保护头不是合法 JSON") from exc
        meta_mod.validate_protected_header(header, file_id=file_id)
        return outer, header, dek, mk

    def decrypt_stream(
        self,
        file_id: str,
        dest: BinaryIO,
        *,
        verify_chunk_size: bool = True,
    ) -> dict:
        """把密文流式认证解密到 ``dest``（不落临时明文）。

        每个块解密时 GCM 即完成认证；全部读完后再核对块数与总长度，
        因此截断、追加、交换块都会在写入完成前 / 完成时被发现并抛错。
        返回受保护头信息字典。
        """
        with self._lock:
            _, header, dek, _ = self._open_envelope(file_id)
            _, blob_path = self._paths(file_id)
            if not blob_path.exists():
                raise FileNotFoundError(f"数据块文件缺失：{file_id}")

            chunk_size = header["chunk_size"]
            blocks = header["blocks"]
            size = header["plaintext_size"]
            written = 0
            with open(blob_path, "rb") as src:
                for index, chunk in blob.iter_blocks(
                    src,
                    dek=dek,
                    file_id=file_id,
                    chunk_size=chunk_size,
                    expected_blocks=blocks,
                ):
                    if verify_chunk_size:
                        if index < blocks - 1 and len(chunk) != chunk_size:
                            raise CorruptContainerError(
                                f"第 {index} 块不是满块，块大小与受保护头不一致"
                            )
                        if index == blocks - 1 and not (
                            0 < len(chunk) <= chunk_size
                            if size > 0
                            else False
                        ):
                            raise CorruptContainerError("最后一块大小非法")
                    dest.write(chunk)
                    written += len(chunk)
            if written != size:
                raise CorruptContainerError(
                    f"解密出的明文长度 {written} 与受保护头声明的 {size} 不一致"
                )
            dest.flush()
            return dict(header)

    def decrypt_file(
        self, file_id: str, output_path: str | os.PathLike[str]
    ) -> Path:
        """解密为磁盘文件：明文先写入同目录 0600 临时文件，校验全部通过后
        原子 rename 为目标文件；任何失败都安全覆写并删除临时明文。"""
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with plaintext_temp_file(output_path.parent) as (tmp_fh, commit):
            self.decrypt_stream(file_id, tmp_fh)
            commit(output_path)
        return output_path

    # ------------------------------------------------------------------ 轮换

    def rotate_master_key(
        self,
        file_id: str,
        *,
        new_kid: str | None = None,
    ) -> RotationInfo:
        """把文件的数据密钥重新包裹到新主密钥下。

        只重写 meta（原子替换），blob 完全不动。DEK 在内存中短暂存在，
        绝不写入磁盘。目标密钥缺省时生成一把新主密钥。
        """
        with self._lock:
            outer, header, dek, old_mk = self._open_envelope(file_id)
            old_kid = old_mk.kid
            if new_kid is None:
                new_mk = self.create_master_key()
            else:
                new_mk = self.key_store.get(new_kid)
            if new_mk.kid == old_kid:
                raise RotationError("新主密钥与当前主密钥相同，无需轮换")

            counter = self.key_store.allocate_wrap_counter()
            meta_bytes = meta_mod.seal_meta(
                file_id=file_id,
                kid=new_mk.kid,
                dek=dek,
                master_key=new_mk.key_material,
                plaintext_size=header["plaintext_size"],
                chunk_size=header["chunk_size"],
                blocks=header["blocks"],
                header_version=header["header_version"] + 1,
                wrap_counter=counter,
            )

            meta_path, blob_path = self._paths(file_id)
            blob_before = blob_path.read_bytes()
            meta_fh, tmp_meta = open_secure_temp(self.meta_dir, prefix=".meta.")
            try:
                meta_fh.write(meta_bytes)
                fsync_file(meta_fh)
                meta_fh.close()

                # 故障注入：模拟"替换前崩溃"。旧 meta 保持完好，可直接重试。
                if self.crash_hook is not None:
                    self.crash_hook("rotate-before-replace", file_id)

                commit_temp(tmp_meta, meta_path)
            except BaseException:
                try:
                    meta_fh.close()
                except OSError:
                    pass
                secure_unlink(tmp_meta)
                raise

            # 轮换后自检：blob 必须与轮换前逐字节相同。
            if blob_path.read_bytes() != blob_before:
                raise CorruptContainerError("轮换后数据块文件发生了变化（不应发生）")

            return RotationInfo(
                file_id=file_id,
                old_kid=old_kid,
                new_kid=new_mk.kid,
                header_version=header["header_version"] + 1,
                blob_bytes_changed=0,
            )

    # ------------------------------------------------------------------ 查询

    def describe(self, file_id: str) -> dict:
        """返回文件的信封信息（含受保护头与包裹主密钥 ID）。"""
        with self._lock:
            outer, header, _, mk = self._open_envelope(file_id)
            return {
                "file_id": file_id,
                "wrapped_by_kid": outer["kid"],
                "wrapping_key_created_at": mk.created_at,
                "header_version": header["header_version"],
                "algorithm": header["alg"],
                "plaintext_size": header["plaintext_size"],
                "chunk_size": header["chunk_size"],
                "blocks": header["blocks"],
            }

    def list_files(self) -> list[str]:
        """列出存储中全部文件 ID（按 meta 文件枚举）。"""
        result = []
        for entry in sorted(self.meta_dir.iterdir()):
            name = entry.name
            if name.endswith(".meta"):
                fid = name[: -len(".meta")]
                if _valid_file_id(fid):
                    result.append(fid)
        return result
