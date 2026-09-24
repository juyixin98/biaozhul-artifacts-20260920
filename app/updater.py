"""更新验证核心 + 本地信任状态的原子存储。

验证链条（从外到内的信任链，全部通过才接受）::

    root（信任锚，轮换时需旧 root 与新 root 双授权）
      └─ timestamp：版本不得回退；不得过期；其中的哈希/长度绑定 snapshot 字节
           └─ snapshot：版本不得回退；不得过期；哈希必须与字节一致；
                        meta 版本绑定 targets
                └─ targets：版本不得回退；不得过期；哈希必须与字节一致；
                            每个附带的目标文件必须满足 length+sha256 绑定

任意一步失败都抛 :class:`UpdateRejected`，且在原子提交点之前不会触碰
已提交的状态文件，因此“root 轮换中断”不会留下半更新状态。
"""

from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import os
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterator

from . import metadata as md
from .crypto import verify_signature
from .errors import (
    BindingError,
    ExpiredMetadataError,
    HashMismatchError,
    MetadataError,
    NoStateError,
    RollbackError,
    SignatureError,
    StateExistsError,
    TargetNotFoundError,
)

# 单份元数据 / 单个目标文件的体积上限，防止耗尽磁盘或内存
MAX_METADATA_SIZE = 1 * 1024 * 1024  # 1 MiB
MAX_TARGET_SIZE = 100 * 1024 * 1024  # 100 MiB

STATE_FILENAME = "trust-state.json"
LOCK_FILENAME = ".lock"
STORE_DIRNAME = "store"
_TMP_SUFFIX = ".tmp"


# ---------------------------------------------------------------------------
# 更新包与状态
# ---------------------------------------------------------------------------


@dataclass
class Bundle:
    """客户端提交的一次更新：四段元数据（root 可空）+ 目标文件。"""

    timestamp: bytes
    snapshot: bytes
    targets: bytes
    root: bytes | None = None
    # 目标名 -> 文件字节。只允许出现在 targets 元数据中且哈希匹配的文件。
    files: dict[str, bytes] = field(default_factory=dict)
    # 测试钩子：在“验证全部通过、原子提交之前”模拟进程崩溃
    fail_before_commit: bool = False


@dataclass(frozen=True)
class _Validated:
    root: md.Metadata
    targets: md.Metadata
    snapshot: md.Metadata
    timestamp: md.Metadata


class TrustState:
    """已提交的信任状态。字段全部来自 ``trust-state.json``。

    bootstrap 之后、首个更新之前，targets/snapshot/timestamp 可能为 None。
    """

    def __init__(
        self,
        root_raw: bytes,
        targets_raw: bytes | None,
        snapshot_raw: bytes | None,
        timestamp_raw: bytes | None,
    ) -> None:
        self.root = md.parse_envelope(root_raw, md.ROOT)
        self.targets = (
            md.parse_envelope(targets_raw, md.TARGETS) if targets_raw else None
        )
        self.snapshot = (
            md.parse_envelope(snapshot_raw, md.SNAPSHOT) if snapshot_raw else None
        )
        self.timestamp = (
            md.parse_envelope(timestamp_raw, md.TIMESTAMP) if timestamp_raw else None
        )

    @property
    def versions(self) -> dict[str, int]:
        result = {md.ROOT: self.root.signed["version"]}
        for role, piece in (
            (md.TARGETS, self.targets),
            (md.SNAPSHOT, self.snapshot),
            (md.TIMESTAMP, self.timestamp),
        ):
            if piece is not None:
                result[role] = piece.signed["version"]
        return result

    def target(self, name: str) -> md.FileMeta:
        if self.targets is None:
            raise TargetNotFoundError("尚未提交任何 targets 元数据")
        targets = md.target_map(self.targets.signed)
        try:
            return targets[name]
        except KeyError:
            raise TargetNotFoundError(f"目标 {name} 不在当前可信清单中") from None


# ---------------------------------------------------------------------------
# 纯验证逻辑（不碰磁盘，便于单元测试）
# ---------------------------------------------------------------------------


def _check_role_signatures(
    metadata_obj: md.Metadata, root_signed: dict, role: str
) -> None:
    """按 root 中该角色的授权检查签名阈值。

    与 TUF 一致：未被授权的 keyid 一律忽略；授权 keyid 中只要存在一条
    合法签名即计数一次（同一把钥匙的多条签名不重复计数）；计数达到
    threshold 才算通过。签名十六进制损坏按“该签名无效”处理。
    """

    authorized, threshold = md.role_keys(root_signed, role)
    payload = md.signed_bytes(metadata_obj)
    good: set[str] = set()
    for sig in metadata_obj.signatures:
        keyid = sig["keyid"]
        if keyid not in authorized or keyid in good:
            continue
        public_hex = md.root_public_hex(root_signed, keyid)
        try:
            verify_signature(public_hex, sig["sig"], payload)
        except SignatureError:
            continue
        good.add(keyid)
    if len(good) < threshold:
        raise SignatureError(
            f"{role}: 有效签名数 {len(good)} 低于阈值 {threshold}"
        )


def _check_expiry(metadata_obj: md.Metadata, now: datetime) -> None:
    if md.is_expired(metadata_obj.signed, now):
        raise ExpiredMetadataError(
            f"{metadata_obj.signed['_type']}: 元数据已过期 "
            f"(expires={metadata_obj.signed['expires']})，拒绝使用"
        )


def _expired(metadata_obj: md.Metadata) -> ExpiredMetadataError:
    return ExpiredMetadataError(
        f"{metadata_obj.signed['_type']}: 元数据已过期 "
        f"(expires={metadata_obj.signed['expires']})，拒绝使用"
    )


def _verify_hash_length(name: str, data: bytes, meta_obj: md.FileMeta) -> None:
    if len(data) != meta_obj.length:
        raise HashMismatchError(
            f"{name}: 长度 {len(data)} 与声明的 {meta_obj.length} 不符"
        )
    digest = hashlib.sha256(data).hexdigest()
    if digest != meta_obj.sha256:
        raise HashMismatchError(f"{name}: sha256 与声明不符（内容被替换？）")


def _require_newer(role: str, new_version: int, old_version: int) -> None:
    if new_version < old_version:
        raise RollbackError(
            f"{role}: 试图回滚版本 {old_version} -> {new_version}"
        )
    if new_version == old_version:
        # 重放同版本元数据同样拒绝（TUF: version rollback / replay）
        raise RollbackError(f"{role}: 版本 {new_version} 与当前版本相同，拒绝重放")


def _check_root_signed_by_both(
    new_root: md.Metadata, old_root: md.Metadata
) -> None:
    """root 轮换：新 root 必须同时被旧 root 信任链与新 root 信任链签名。"""

    _check_role_signatures(new_root, old_root.signed, md.ROOT)
    _check_role_signatures(new_root, new_root.signed, md.ROOT)


def validate_bootstrap(root_raw: bytes, now: datetime) -> md.Metadata:
    """引导：自签名的 root 即初始信任锚（带外分发，须未过期）。"""

    root = md.parse_envelope(root_raw, md.ROOT)
    if len(root_raw) > MAX_METADATA_SIZE:
        raise MetadataError("root: 元数据超过大小上限")
    _check_role_signatures(root, root.signed, md.ROOT)
    _check_expiry(root, now)
    return root


def validate_bundle(
    bundle: Bundle, state: TrustState | None, now: datetime
) -> _Validated:
    """对一次更新做完整验证，返回解析好的四段新元数据。

    ``state`` 为 None 时必须走 bootstrap（本函数不处理引导）。
    """

    if state is None:
        raise NoStateError("尚未引导：请先提交自签名 root")
    if bundle.timestamp is None or bundle.snapshot is None or bundle.targets is None:
        raise MetadataError("更新必须同时提供 timestamp/snapshot/targets")

    # 1) root（可选轮换）。版本只能 +1，且需要新旧双方授权。
    if bundle.root is not None:
        if len(bundle.root) > MAX_METADATA_SIZE:
            raise MetadataError("root: 元数据超过大小上限")
        new_root = md.parse_envelope(bundle.root, md.ROOT)
        old_version = state.root.signed["version"]
        new_version = new_root.signed["version"]
        if new_version <= old_version:
            raise RollbackError(
                f"root: 试图回滚/重放版本 {old_version} -> {new_version}"
            )
        if new_version != old_version + 1:
            raise RollbackError(
                f"root: 不允许跳过版本（{old_version} -> {new_version}，"
                f"必须先升级到 v{old_version + 1}）"
            )
        _check_root_signed_by_both(new_root, state.root)
        _check_expiry(new_root, now)
        root_for_roles = new_root
    else:
        root_for_roles = state.root

    # 2) 其余三段：体积、结构解析
    pieces = {
        md.TIMESTAMP: bundle.timestamp,
        md.SNAPSHOT: bundle.snapshot,
        md.TARGETS: bundle.targets,
    }
    for role, raw in pieces.items():
        if len(raw) > MAX_METADATA_SIZE:
            raise MetadataError(f"{role}: 元数据超过大小上限")
    timestamp = md.parse_envelope(bundle.timestamp, md.TIMESTAMP)
    snapshot = md.parse_envelope(bundle.snapshot, md.SNAPSHOT)
    targets = md.parse_envelope(bundle.targets, md.TARGETS)

    # 3) 签名（用轮换后的新 root 授权验证 timestamp/snapshot/targets）
    _check_role_signatures(timestamp, root_for_roles.signed, md.TIMESTAMP)
    _check_role_signatures(snapshot, root_for_roles.signed, md.SNAPSHOT)
    _check_role_signatures(targets, root_for_roles.signed, md.TARGETS)

    # 4) 过期检查（冻结旧 timestamp 的核心防线）。
    #    必须先于版本回滚检查：冻结攻击的特征就是“签名合法的旧元数据”，
    #    必须以 expired 明确拒绝，而不是被回滚规则抢先拦住。
    for metadata_obj in (timestamp, snapshot, targets):
        _check_expiry(metadata_obj, now)

    # 5) 版本回退/重放检查（第二道防线，相对当前已提交状态）。
    #    引导后的首个更新里三段版本必须从 1 开始。
    current = {
        md.TIMESTAMP: state.timestamp,
        md.SNAPSHOT: state.snapshot,
        md.TARGETS: state.targets,
    }
    incoming = {
        md.TIMESTAMP: timestamp,
        md.SNAPSHOT: snapshot,
        md.TARGETS: targets,
    }
    for role in (md.TIMESTAMP, md.SNAPSHOT, md.TARGETS):
        new_version = incoming[role].signed["version"]
        if current[role] is None:
            if new_version != 1:
                raise RollbackError(
                    f"{role}: 首个版本必须是 v1，收到 v{new_version}"
                )
        else:
            _require_newer(
                role, new_version, current[role].signed["version"]
            )

    # 6) 跨角色哈希/长度/版本绑定（防混搭历史文件）
    snap_meta = md.file_meta(timestamp.signed["meta"]["snapshot.json"])
    _verify_hash_length("snapshot.json", snapshot.raw, snap_meta)
    if snapshot.signed["version"] != timestamp.signed["meta"]["snapshot.json"]["version"]:
        raise BindingError(
            "timestamp 声明的 snapshot 版本与 snapshot 自身版本不一致"
        )

    targets_meta = md.file_meta(snapshot.signed["meta"]["targets.json"])
    _verify_hash_length("targets.json", targets.raw, targets_meta)
    if targets.signed["version"] != snapshot.signed["meta"]["targets.json"]["version"]:
        raise BindingError(
            "snapshot 声明的 targets 版本与 targets 自身版本不一致"
        )

    # 7) 目标文件绑定：声明集合必须与随附文件集合一致，逐个校验
    declared = md.target_map(targets.signed)
    if set(bundle.files) != set(declared):
        missing = sorted(set(declared) - set(bundle.files))
        extra = sorted(set(bundle.files) - set(declared))
        raise BindingError(
            f"随附目标文件与 targets 声明不一致（缺少 {missing}，多出 {extra}）"
        )
    for name, data in bundle.files.items():
        _validate_target_name(name)
        if len(data) > MAX_TARGET_SIZE:
            raise MetadataError(f"目标 {name}: 超过大小上限")
        _verify_hash_length(f"目标 {name}", data, declared[name])

    return _Validated(
        root=root_for_roles, targets=targets,
        snapshot=snapshot, timestamp=timestamp,
    )


def _validate_target_name(name: str) -> None:
    if not name or name in (".", "..") or os.path.basename(name) != name:
        raise MetadataError(f"非法目标文件名: {name!r}（不允许路径分隔符）")
    if "\x00" in name:
        raise MetadataError(f"非法目标文件名: {name!r}（含 NUL）")


# ---------------------------------------------------------------------------
# 磁盘存储：内容寻址 + 单文件状态原子替换 + 文件锁
# ---------------------------------------------------------------------------


class TrustStore:
    """管理数据目录::

    data/
      trust-state.json      当前唯一可信状态（临时文件 + rename 原子替换）
      store/<sha256>        内容寻址的目标文件（同样原子落盘）
      .lock                 进程间互斥锁
    """

    def __init__(self, data_dir: str) -> None:
        self.data_dir = data_dir
        self.store_dir = os.path.join(data_dir, STORE_DIRNAME)
        self.state_path = os.path.join(data_dir, STATE_FILENAME)
        self.lock_path = os.path.join(data_dir, LOCK_FILENAME)
        os.makedirs(self.store_dir, exist_ok=True)

    # ----- 锁与状态读写 -----

    @contextmanager
    def _locked(self) -> Iterator[None]:
        # 调用方已确保 data_dir 存在
        fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def load(self) -> TrustState | None:
        try:
            with open(self.state_path, "r", encoding="utf-8") as fh:
                doc = json.load(fh)
        except FileNotFoundError:
            return None

        def _raw(role: str) -> bytes | None:
            value = doc.get(role, "")
            return bytes.fromhex(value) if value else None

        return TrustState(
            root_raw=bytes.fromhex(doc["root"]),
            targets_raw=_raw(md.TARGETS),
            snapshot_raw=_raw(md.SNAPSHOT),
            timestamp_raw=_raw(md.TIMESTAMP),
        )

    def _atomic_write_state(self, validated: _Validated) -> None:
        doc = {
            "format": 1,
            md.ROOT: validated.root.raw.hex(),
            md.TARGETS: validated.targets.raw.hex(),
            md.SNAPSHOT: validated.snapshot.raw.hex(),
            md.TIMESTAMP: validated.timestamp.raw.hex(),
        }
        _atomic_write_text(
            self.state_path,
            json.dumps(doc, separators=(",", ":"), sort_keys=True),
        )

    # ----- 对外操作 -----

    def bootstrap(self, root_raw: bytes, now: datetime | None = None) -> dict:
        now = now or datetime.now(timezone.utc)
        with self._locked():
            if self.load() is not None:
                raise StateExistsError("已经引导过；如需更换信任锚请走 root 轮换")
            root = validate_bootstrap(root_raw, now)
            _atomic_write_text(
                self.state_path,
                json.dumps(
                    {
                        "format": 1,
                        md.ROOT: root.raw.hex(),
                        md.TARGETS: "",
                        md.SNAPSHOT: "",
                        md.TIMESTAMP: "",
                    },
                    separators=(",", ":"),
                    sort_keys=True,
                ),
            )
        return {"status": "bootstrapped", "root_version": root.signed["version"]}

    def apply_update(
        self, bundle: Bundle, now: datetime | None = None
    ) -> dict:
        """完整验证一次更新并原子提交；任何失败都不会改变已提交状态。"""

        now = now or datetime.now(timezone.utc)
        with self._locked():
            state = self.load()
            if state is None:
                raise NoStateError("尚未引导：请先 POST /api/bootstrap")
            validated = validate_bundle(bundle, state, now)

            # 先把目标文件以“临时名 -> 内容寻址名”原子放进仓库。
            staged: list[tuple[str, str]] = []  # (最终路径, 临时路径)
            for name, data in bundle.files.items():
                digest = hashlib.sha256(data).hexdigest()
                final_path = os.path.join(self.store_dir, digest)
                tmp_fd, tmp_path = tempfile.mkstemp(
                    dir=self.store_dir, suffix=_TMP_SUFFIX
                )
                try:
                    with os.fdopen(tmp_fd, "wb") as fh:
                        fh.write(data)
                        fh.flush()
                        os.fsync(fh.fileno())
                    os.replace(tmp_path, final_path)
                except BaseException:
                    _unlink_quiet(tmp_path)
                    raise
                staged.append((final_path, tmp_path))

            # 测试钩子：此刻所有新文件已就位，但状态尚未切换 -> 模拟崩溃。
            if bundle.fail_before_commit:
                raise RuntimeError("fail_before_commit：模拟提交前崩溃")

            # 唯一的信任状态切换点：rename 原子替换。
            self._atomic_write_state(validated)

        return {
            "status": "updated",
            "versions": {
                md.ROOT: validated.root.signed["version"],
                md.TARGETS: validated.targets.signed["version"],
                md.SNAPSHOT: validated.snapshot.signed["version"],
                md.TIMESTAMP: validated.timestamp.signed["version"],
            },
            "targets": sorted(md.target_map(validated.targets.signed)),
        }

    def reset(self) -> None:
        """清空全部可信状态与目标仓库（测试/演示用）。"""

        with self._locked():
            _unlink_quiet(self.state_path)
            for name in os.listdir(self.store_dir):
                _unlink_quiet(os.path.join(self.store_dir, name))

    def status(self) -> dict:
        with self._locked():
            state = self.load()
        if state is None:
            return {"bootstrapped": False, "versions": {}, "targets": []}
        targets = (
            md.target_map(state.targets.signed) if state.targets else {}
        )
        return {
            "bootstrapped": True,
            "versions": state.versions,
            "targets": sorted(targets),
        }

    def open_target(self, name: str) -> tuple[bytes, md.FileMeta]:
        """下载：目标名必须在当前可信清单中，返回字节时再核对一次哈希。"""

        with self._locked():
            state = self.load()
            if state is None:
                raise NoStateError("尚未引导")
            meta_obj = state.target(name)
            _validate_target_name(name)
            path = os.path.join(self.store_dir, meta_obj.sha256)
            try:
                with open(path, "rb") as fh:
                    data = fh.read()
            except FileNotFoundError:
                raise TargetNotFoundError(
                    f"目标 {name} 已被当前清单声明，但内容尚未随更新下载"
                ) from None
        _verify_hash_length(f"目标 {name}", data, meta_obj)
        return data, meta_obj


def _atomic_write_text(path: str, text: str) -> None:
    directory = os.path.dirname(path)
    fd, tmp_path = tempfile.mkstemp(dir=directory, suffix=_TMP_SUFFIX)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, path)
        # fsync 目录，确保 rename 在崩溃后也持久化
        dir_fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except BaseException:
        _unlink_quiet(tmp_path)
        raise


def _unlink_quiet(path: str) -> None:
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    except OSError as exc:
        if exc.errno != errno.ENOENT:
            raise
