"""内存中的“更新仓库”构建器。

测试与演示用：持有四个角色（可跨版本轮换）的签名私钥，
能发布一组合法元数据、推进版本号，也能产出各种攻击包。
这是*发布端*代码，和验证端刻意分开。
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from . import crypto_utils, metadata as meta
from .verifier import SNAPSHOT_TARGETS_KEY, UpdateInput


def _fut(days: int = 30) -> str:
    return meta.iso_utc(datetime.now(timezone.utc) + timedelta(days=days))


def _past(days: int = 1) -> str:
    return meta.iso_utc(datetime.now(timezone.utc) - timedelta(days=days))


@dataclass
class RoleKeys:
    priv_hexes: list[str]
    threshold: int = 1

    @property
    def pub_hexes(self) -> list[str]:
        return [crypto_utils.public_from_private(p) for p in self.priv_hexes]


@dataclass
class Repository:
    roles: dict[str, RoleKeys]
    root_version: int = 1
    timestamp_version: int = 1
    snapshot_version: int = 1
    targets_version: int = 1
    files: dict[str, bytes] = field(default_factory=dict)
    expires: dict[str, str] = field(default_factory=dict)
    # 已发布的最新元数据信封（bytes）
    latest: dict[str, bytes] = field(default_factory=dict)

    @classmethod
    def create(cls, *, root_extra_key: bool = False) -> "Repository":
        roles = {}
        for role in ("root", "targets", "snapshot", "timestamp"):
            roles[role] = RoleKeys([crypto_utils.generate_keypair()[0]])
        if root_extra_key:
            roles["root"].priv_hexes.append(crypto_utils.generate_keypair()[0])
        repo = cls(roles=roles)
        repo.expires = {role: _fut() for role in ("root", "targets", "snapshot", "timestamp")}
        repo._publish_root()
        repo.publish({"app.bin": b"app-version-1"})
        return repo

    # ---- 发布 -----------------------------------------------------------

    def root_pub_hex(self) -> str:
        return self.roles["root"].pub_hexes[0]

    def _root_signed(self) -> dict:
        keys = {}
        role_specs = {}
        for role, rk in self.roles.items():
            keyids = []
            for pub in rk.pub_hexes:
                obj = crypto_utils.make_key_object(pub)
                keys[obj["keyid"]] = obj
                keyids.append(obj["keyid"])
            role_specs[role] = {"keyids": keyids, "threshold": rk.threshold}
        return meta.build_signed("root", self.root_version, self.expires["root"],
                                 {"keys": keys, "roles": role_specs,
                                  "consistent_snapshot": False})

    def _publish_root(self) -> bytes:
        signed = self._root_signed()
        raw = meta.canonical(meta.sign_signed(signed, self.roles["root"].priv_hexes))
        self.latest["root"] = raw
        return raw

    def build_root_envelope(self, roles: dict[str, RoleKeys], version: int,
                            signer_priv_hexes: list[str]) -> bytes:
        """用指定角色集合/版本号构造 root 信封并由指定密钥签名，*不改变自身状态*。

        用于攻击测试：例如让一张 v2 root 只被新密钥自签。
        """
        saved_roles, saved_version = self.roles, self.root_version
        try:
            self.roles = roles
            self.root_version = version
            signed = self._root_signed()
            return meta.canonical(meta.sign_signed(signed, signer_priv_hexes))
        finally:
            self.roles, self.root_version = saved_roles, saved_version

    def set_expiry(self, role: str, expires: str) -> None:
        self.expires[role] = expires

    def publish(self, files: dict[str, bytes], *, bump: tuple[str, ...] = ()) -> dict[str, bytes]:
        """发布一组新目标文件，默认各元数据版本不变；bump 指定推进哪些角色版本。"""
        self.files = dict(files)
        for role in bump:
            attr = f"{role}_version"
            setattr(self, attr, getattr(self, attr) + 1)

        # targets
        targets_map = {
            name: {"length": len(data), "hashes": meta.file_hashes(data)}
            for name, data in sorted(self.files.items())
        }
        targets_signed = meta.build_signed(
            "targets", self.targets_version, self.expires["targets"],
            {"targets": targets_map})
        targets_env = meta.sign_signed(targets_signed, self.roles["targets"].priv_hexes)
        targets_raw = meta.canonical(targets_env)
        self.latest["targets"] = targets_raw

        # snapshot
        snapshot_signed = meta.build_signed(
            "snapshot", self.snapshot_version, self.expires["snapshot"],
            {"meta": {
                SNAPSHOT_TARGETS_KEY: {
                    "version": self.targets_version,
                    "length": len(targets_raw),
                    "hashes": {"sha256": meta.sha256_hex(targets_raw),
                               "sha512": meta.sha512_hex(targets_raw)},
                }}})
        snapshot_env = meta.sign_signed(snapshot_signed, self.roles["snapshot"].priv_hexes)
        snapshot_raw = meta.canonical(snapshot_env)
        self.latest["snapshot"] = snapshot_raw

        # timestamp
        timestamp_signed = meta.build_signed(
            "timestamp", self.timestamp_version, self.expires["timestamp"],
            {"meta": {
                "snapshot": {
                    "version": self.snapshot_version,
                    "length": len(snapshot_raw),
                    "hashes": {"sha256": meta.sha256_hex(snapshot_raw),
                               "sha512": meta.sha512_hex(snapshot_raw)},
                }}})
        timestamp_env = meta.sign_signed(timestamp_signed, self.roles["timestamp"].priv_hexes)
        timestamp_raw = meta.canonical(timestamp_env)
        self.latest["timestamp"] = timestamp_raw
        return dict(self.latest)

    def rotate_root(self, new_roles: dict[str, RoleKeys] | None = None,
                    *, signer_override: dict[str, RoleKeys] | None = None) -> bytes:
        """推进 root 版本。

        默认用当前 root 私钥签名新 root（合法轮换）。
        new_roles 给出轮换后的四角色密钥集合；signer_override 可强制用
        其它密钥签名（用于攻击测试）。
        """
        old_root = self.roles["root"]
        if new_roles is not None:
            self.roles = new_roles
        self.root_version += 1
        signed = self._root_signed()
        signers = (signer_override["root"].priv_hexes if signer_override
                   else old_root.priv_hexes)
        raw = meta.canonical(meta.sign_signed(signed, signers))
        self.latest["root"] = raw
        # 重新签发其余元数据，使新角色密钥能为它们背书
        self.publish(dict(self.files))
        return raw

    def bundle(self, *, include_root: bool = True,
               files: dict[str, bytes] | None = None,
               override: dict[str, bytes] | None = None) -> UpdateInput:
        """构造一次更新提交。override 可替换任意角色元数据字节（攻击注入）。"""
        raws = {k: bytes(v) for k, v in self.latest.items()}
        if override:
            raws.update(override)
        payload_files = self.files if files is None else files
        return UpdateInput(
            timestamp=raws["timestamp"],
            snapshot=raws["snapshot"],
            targets=raws["targets"],
            files=dict(payload_files),
            root=raws["root"] if include_root else None,
        )

    # ---- 攻击包构造 ------------------------------------------------------

    def freeze_old_timestamp(self, previous_bundle: UpdateInput) -> UpdateInput:
        """混搭：新 snapshot/targets/root + 旧时间戳（冻结重放）。"""
        b = self.bundle()
        b.timestamp = previous_bundle.timestamp
        return b

    def mix_historical(self, previous_bundle: UpdateInput) -> UpdateInput:
        """混搭：当前时间戳 + 上一代 snapshot/targets 文件。"""
        b = self.bundle()
        b.snapshot = previous_bundle.snapshot
        b.targets = previous_bundle.targets
        b.files = dict(previous_bundle.files)
        return b

    def tamper_file(self, name: str, new_content: bytes) -> UpdateInput:
        """只替换目标文件内容，元数据仍声明旧哈希。"""
        files = dict(self.files)
        files[name] = new_content
        return self.bundle(files=files)

    def tamper_metadata_unsigned(self, role: str, mutate) -> UpdateInput:
        """直接改 envelope.signed 但保留旧签名（签名验证应失败）。"""
        env = json.loads(self.latest[role].decode("utf-8"))
        mutate(env["signed"])
        override = {role: meta.canonical(env)}
        return self.bundle(override=override)

    def stale_signature_for_modified(self, role: str, mutate) -> UpdateInput:
        """角色内容被重签以通过上层哈希绑定，却故意携带一张对旧内容的签名。

        等价于：合法发布者更新到一半被中断、或攻击者拿到旧签名块嫁接到
        新内容上。上层（timestamp/snapshot）用重算后的绑定重新签发，
        因此能到达本角色的签名校验，并在那里因签名不匹配而失败。
        """
        env = json.loads(self.latest[role].decode("utf-8"))
        old_signatures = copy.deepcopy(env["signatures"])
        mutate(env["signed"])
        env["signatures"] = old_signatures
        raw = meta.canonical(env)
        override = {role: raw}
        # 重签上层绑定，使攻击包能穿过哈希链到达本角色的签名校验
        if role == "targets":
            snap_signed = json.loads(self.latest["snapshot"].decode("utf-8"))["signed"]
            snap_signed["meta"][SNAPSHOT_TARGETS_KEY] = {
                "version": env["signed"]["version"], "length": len(raw),
                "hashes": {"sha256": meta.sha256_hex(raw),
                           "sha512": meta.sha512_hex(raw)}}
            snap_env = meta.sign_signed(snap_signed, self.roles["snapshot"].priv_hexes)
            snap_raw = meta.canonical(snap_env)
            override["snapshot"] = snap_raw
            ts_signed = json.loads(self.latest["timestamp"].decode("utf-8"))["signed"]
            ts_signed["meta"]["snapshot"] = {
                "version": snap_signed["version"], "length": len(snap_raw),
                "hashes": {"sha256": meta.sha256_hex(snap_raw),
                           "sha512": meta.sha512_hex(snap_raw)}}
            override["timestamp"] = meta.canonical(
                meta.sign_signed(ts_signed, self.roles["timestamp"].priv_hexes))
        return self.bundle(override=override)

    def rollback_signed(self, role: str, old_raw: bytes) -> UpdateInput:
        """把某个角色整体换成旧版本（回滚攻击）。"""
        return self.bundle(override={role: old_raw})


def bump_all(repo: Repository, files: dict[str, bytes]) -> dict[str, bytes]:
    """发布并推进 timestamp/snapshot/targets 三个版本。"""
    return repo.publish(files, bump=("timestamp", "snapshot", "targets"))
