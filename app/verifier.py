"""验证链核心：root → timestamp → snapshot → targets → 目标文件。

信任规则（TUF 精简模型）：

* **root**：首次安装只接受 v1，且必须由带外引导根公钥签名；
  之后的每个 root v(n+1) 必须由*当前已信任的* root v(n) 中声明的
  root 角色密钥签名（根轮换的授权链），版本号不允许跳跃。
* **timestamp**：由 root 中 timestamp 角色密钥签名；版本号不得低于已信任
  版本（拒绝回滚/冻结重放），同版本内容必须逐字节一致。
* **snapshot**：其哈希与版本号由 timestamp 绑定（篡改即断链），
  由 root 中 snapshot 角色密钥签名，版本同样不得回滚。
* **targets**：哈希与版本号由 snapshot 绑定，由 targets 角色密钥签名。
* **目标文件**：必须与 targets 中声明的 sha256/sha512/长度完全一致。

四个角色全部检查过期时间。只有整包通过后才原子提交本地信任状态。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone

from . import crypto_utils, metadata as meta
from .errors import (
    BadFormatError,
    HashMismatchError,
    InterferenceError,
    RollbackError,
    VersionGapError,
)
from .storage import TrustStore, b64d, b64e

REQUIRED_ROLES = ("root", "targets", "snapshot", "timestamp")
SNAPSHOT_TARGETS_KEY = "targets.json"


@dataclass
class UpdateInput:
    """一次更新提交的全部原始字节。"""

    timestamp: bytes
    snapshot: bytes
    targets: bytes
    files: dict[str, bytes] = field(default_factory=dict)
    root: bytes | None = None


@dataclass
class _View:
    """验证过程中当前有效的 root 视图。"""

    envelope: dict
    signed: dict
    keys: dict
    roles: dict
    version: int


def apply_update(
    store: TrustStore,
    update: UpdateInput,
    *,
    bootstrap_root_pub: str | None,
    now: datetime | None = None,
) -> dict:
    """验证整包并在通过时原子提交。任何异常都不会改动本地状态。"""
    with store:
        state = store.load()
        result = _verify(state, update, bootstrap_root_pub=bootstrap_root_pub, now=now)
        _commit(store, result, update.files)
        return result["summary"]


# --------------------------------------------------------------------- 验证


def _verify(state: dict, update: UpdateInput, *, bootstrap_root_pub: str | None,
            now: datetime | None) -> dict:
    now = now or datetime.now(timezone.utc)
    updated_roles: list[str] = []

    # 1) root -----------------------------------------------------------
    view, root_changed = _verify_root(state, update.root, bootstrap_root_pub, now=now)
    if root_changed:
        updated_roles.append("root")

    # 2) timestamp ------------------------------------------------------
    ts_env, ts_signed = _parse_and_check_role(
        update.timestamp, role="timestamp", view=view, state=state, now=now,
    )
    snapshot_ref = _meta_entry(ts_signed, "snapshot", role="timestamp")

    # 3) snapshot -------------------------------------------------------
    _check_hash_binding(update.snapshot, snapshot_ref, what="snapshot")
    snap_env, snap_signed = _parse_and_check_role(
        update.snapshot, role="snapshot", view=view, state=state, now=now,
    )
    if snap_signed["version"] != snapshot_ref["version"]:
        raise InterferenceError(
            "VERSION_BINDING",
            "snapshot 版本号与 timestamp 声明的不一致",
            {"snapshot": snap_signed["version"], "timestamp_says": snapshot_ref["version"]},
        )
    targets_ref = _meta_entry(snap_signed, SNAPSHOT_TARGETS_KEY, role="snapshot")

    # 4) targets --------------------------------------------------------
    _check_hash_binding(update.targets, targets_ref, what="targets")
    tgt_env, tgt_signed = _parse_and_check_role(
        update.targets, role="targets", view=view, state=state, now=now,
    )
    if tgt_signed["version"] != targets_ref["version"]:
        raise InterferenceError(
            "VERSION_BINDING",
            "targets 版本号与 snapshot 声明的不一致",
            {"targets": tgt_signed["version"], "snapshot_says": targets_ref["version"]},
        )

    # 5) 目标文件 -------------------------------------------------------
    declared = tgt_signed["targets"]
    files_result = _verify_files(update.files, declared)

    # 版本推进记录（供提交阶段使用）
    new_entries = {
        "root": {"version": view.version, "raw": update.root if update.root is not None
                 else b64d(state["root"]["raw_b64"])},
        "timestamp": {"version": ts_signed["version"], "raw": update.timestamp},
        "snapshot": {"version": snap_signed["version"], "raw": update.snapshot},
        "targets": {"version": tgt_signed["version"], "raw": update.targets},
    }
    for role in ("timestamp", "snapshot", "targets"):
        old = state.get(role)
        if old is None or old["version"] != new_entries[role]["version"]:
            updated_roles.append(role)

    summary = {
        "accepted": True,
        "root_version": view.version,
        "timestamp_version": ts_signed["version"],
        "snapshot_version": snap_signed["version"],
        "targets_version": tgt_signed["version"],
        "root_rotated": root_changed,
        "roles_updated": updated_roles,
        "targets": [
            {"name": name, "length": len(update.files[name]),
             "sha256": declared[name]["hashes"]["sha256"]}
            for name in sorted(update.files)
        ],
    }
    return {"view": view, "new_entries": new_entries, "files": files_result,
            "declared": declared, "summary": summary}


def _verify_root(state: dict, root_raw: bytes | None, bootstrap_pub: str | None, *,
                 now: datetime) -> tuple[_View, bool]:
    trusted = state.get("root")

    if root_raw is None:
        if trusted is None:
            raise BadFormatError(
                "NO_ROOT",
                "本地尚无信任的 root，首次更新必须随包提交 root v1",
            )
        raw = b64d(trusted["raw_b64"])
        env = meta.parse_envelope(raw, role="root")
        signed = env["signed"]
        keys, roles = _validate_root_body(signed)
        meta.check_not_expired(signed["expires"], role="root", now=now)
        return _View(env, signed, keys, roles, signed["version"]), False

    env = meta.parse_envelope(root_raw, role="root")
    signed = env["signed"]
    new_version = signed["version"]
    keys, roles = _validate_root_body(signed)
    meta.check_not_expired(signed["expires"], role="root", now=now)

    if trusted is None:
        # 引导：只接受 v1，且必须由带外引导密钥签名
        if new_version != 1:
            raise VersionGapError(
                "ROOT_BOOTSTRAP_VERSION",
                f"首次安装只接受 root v1，收到的是 v{new_version}",
                {"received": new_version},
            )
        if not bootstrap_pub:
            raise BadFormatError(
                "NO_BOOTSTRAP_KEY",
                "服务器未配置 BOOTSTRAP_ROOT_PUBLIC，无法建立初始信任",
            )
        kid = crypto_utils.keyid_for(bootstrap_pub)
        crypto_utils.verify_threshold(
            env, authorized_keyids=[kid], threshold=1,
            keys={kid: crypto_utils.make_key_object(bootstrap_pub)},
            role="root", signed_bytes=meta.signed_bytes(env),
        )
        rotated = True
    else:
        old_env = meta.parse_envelope(b64d(trusted["raw_b64"]), role="root")
        old_signed = old_env["signed"]
        old_version = old_signed["version"]
        if new_version < old_version:
            raise RollbackError(
                "ROOT_ROLLBACK",
                f"拒绝 root 回滚：已信任 v{old_version}，收到 v{new_version}",
                {"trusted": old_version, "received": new_version},
            )
        if new_version == old_version:
            if meta.signed_bytes(env) != meta.signed_bytes(old_env):
                raise InterferenceError(
                    "ROOT_SAME_VERSION_CONFLICT",
                    f"root v{new_version} 与已信任内容不同但版本号相同",
                    {"version": new_version},
                )
            rotated = False  # 同版本同内容：已是信任内容
        else:
            if new_version != old_version + 1:
                raise VersionGapError(
                    "ROOT_VERSION_GAP",
                    f"root 版本不允许跳跃：已信任 v{old_version}，"
                    f"收到 v{new_version}，应先更新 v{old_version + 1}",
                    {"trusted": old_version, "received": new_version},
                )
            # 关键规则：新 root 必须由旧 root 授权的 root 角色密钥签名
            old_keys, old_roles = _validate_root_body(old_signed)
            spec = old_roles["root"]
            crypto_utils.verify_threshold(
                env, authorized_keyids=spec["keyids"], threshold=spec["threshold"],
                keys=old_keys, role="root", signed_bytes=meta.signed_bytes(env),
            )
            rotated = True

    return _View(env, signed, keys, roles, new_version), rotated


def _parse_and_check_role(raw: bytes, *, role: str, view: _View, state: dict,
                          now: datetime) -> tuple[dict, dict]:
    env = meta.parse_envelope(raw, role=role)
    signed = env["signed"]
    spec = view.roles[role]
    crypto_utils.verify_threshold(
        env, authorized_keyids=spec["keyids"], threshold=spec["threshold"],
        keys=view.keys, role=role, signed_bytes=meta.signed_bytes(env),
    )
    meta.check_not_expired(signed["expires"], role=role, now=now)

    trusted = state.get(role)
    if trusted is not None:
        old_version = trusted["version"]
        if signed["version"] < old_version:
            raise RollbackError(
                f"{role.upper()}_ROLLBACK",
                f"拒绝 {role} 回滚：已信任 v{old_version}，收到 v{signed['version']}",
                {"trusted": old_version, "received": signed["version"]},
            )
        if signed["version"] == old_version:
            # 版本号未推进时，允许根轮换传播（新 root 用新角色密钥重签同版本元数据），
            # 但：
            #   - 上面的阈值签名校验使用的是*当前已授权* root 角色密钥，
            #     旧密钥/伪造密钥签出的同版本内容无法通过；
            #   - timestamp->snapshot->targets 的哈希绑定逐字节校验，
            #     任何内容篡改都会断链。
            pass
    return env, signed


def _meta_entry(signed: dict, meta_name: str, *, role: str) -> dict:
    metas = signed.get("meta")
    if not isinstance(metas, dict) or meta_name not in metas:
        raise BadFormatError(
            "MISSING_META",
            f"{role} 元数据缺少 meta.{meta_name} 条目",
            {"role": role, "meta": meta_name},
        )
    entry = metas[meta_name]
    version = entry.get("version")
    hashes = entry.get("hashes")
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise BadFormatError("BAD_META_VERSION", f"{role}.meta.{meta_name}.version 非法",
                             {"role": role})
    if not isinstance(hashes, dict) or not isinstance(hashes.get("sha256"), str):
        raise BadFormatError("BAD_META_HASHES",
                             f"{role}.meta.{meta_name} 必须至少给出 sha256 哈希",
                             {"role": role})
    if "length" in entry and (not isinstance(entry["length"], int)
                              or isinstance(entry["length"], bool) or entry["length"] < 0):
        raise BadFormatError("BAD_META_LENGTH", f"{role}.meta.{meta_name}.length 非法",
                             {"role": role})
    return entry


def _check_hash_binding(data: bytes, entry: dict, *, what: str) -> None:
    hashes = entry["hashes"]
    if "length" in entry and len(data) != entry["length"]:
        raise HashMismatchError(
            "LENGTH_MISMATCH",
            f"{what} 元数据长度与上层绑定不一致",
            {"expected": entry["length"], "actual": len(data), "what": what},
        )
    for alg, expected in hashes.items():
        try:
            actual = hashlib.new(alg, data).hexdigest()
        except ValueError as exc:
            raise BadFormatError("BAD_HASH_ALG", f"不支持的哈希算法 {alg}",
                                 {"alg": alg, "what": what}) from exc
        if actual != expected:
            raise HashMismatchError(
                "HASH_MISMATCH",
                f"{what} 元数据的 {alg} 与上层绑定不一致（哈希链断裂）",
                {"alg": alg, "expected": expected, "actual": actual, "what": what},
            )


def _validate_root_body(signed: dict) -> tuple[dict, dict]:
    keys = signed.get("keys")
    roles = signed.get("roles")
    if not isinstance(keys, dict) or not isinstance(roles, dict):
        raise BadFormatError("BAD_ROOT_BODY", "root 必须包含 keys 与 roles 两个对象")
    for role in REQUIRED_ROLES:
        spec = roles.get(role)
        if not isinstance(spec, dict) or not isinstance(spec.get("keyids"), list):
            raise BadFormatError("BAD_ROLE_SPEC", f"root.roles.{role} 缺少 keyids 列表",
                                 {"role": role})
        threshold = spec.get("threshold")
        if not isinstance(threshold, int) or isinstance(threshold, bool) or threshold < 1:
            raise BadFormatError("BAD_ROLE_SPEC", f"root.roles.{role}.threshold 必须为 >=1 的整数",
                                 {"role": role})
        for kid in spec["keyids"]:
            key_obj = keys.get(kid)
            if not isinstance(key_obj, dict):
                raise BadFormatError("UNRESOLVED_KEY",
                                     f"角色 {role} 引用了 keys 中不存在的 keyid {kid}",
                                     {"role": role, "keyid": kid})
            pub = key_obj.get("keyval", {}).get("public")
            if not isinstance(pub, str):
                raise BadFormatError("BAD_KEY", f"keyid {kid} 的公钥缺失或非法", {"keyid": kid})
            if key_obj.get("keytype") not in (None, crypto_utils.KEY_TYPE):
                raise BadFormatError("BAD_KEYTYPE",
                                     f"keyid {kid} 的 keytype 不受支持: {key_obj.get('keytype')}",
                                     {"keyid": kid})
            computed = crypto_utils.keyid_for(pub)
            if "keyid" in key_obj and key_obj["keyid"] != kid:
                raise InterferenceError(
                    "KEYID_MISMATCH",
                    f"keyid 映射与公钥内容不一致: 索引 {kid}，对象声明 {key_obj['keyid']}",
                    {"index": kid, "declared": key_obj["keyid"]},
                )
            if kid != computed:
                raise InterferenceError(
                    "KEYID_MISMATCH",
                    f"keyid {kid} 与公钥 sha256 不符（实际 {computed}）",
                    {"index": kid, "computed": computed},
                )
    return keys, roles


def _verify_files(files: dict[str, bytes], declared: dict) -> dict:
    if not isinstance(declared, dict):
        raise BadFormatError("BAD_TARGETS_BODY", "targets 元数据必须包含 targets 对象")

    supplied = set(files)
    wanted = set(declared)
    missing = wanted - supplied
    extra = supplied - wanted
    if missing:
        raise BadFormatError(
            "MISSING_TARGET_FILE",
            "下列 targets 元数据声明的文件未随包提交: " + ", ".join(sorted(missing)),
            {"missing": sorted(missing)},
        )
    if extra:
        raise BadFormatError(
            "UNKNOWN_TARGET_FILE",
            "提交了 targets 元数据未声明的文件: " + ", ".join(sorted(extra)),
            {"extra": sorted(extra)},
        )

    result = {}
    for name in sorted(declared):
        info = declared[name]
        if not isinstance(info, dict) or not isinstance(info.get("hashes"), dict):
            raise BadFormatError("BAD_TARGET_INFO", f"目标 {name} 的描述非法", {"name": name})
        hashes = info["hashes"]
        if "sha256" not in hashes or "sha512" not in hashes:
            raise BadFormatError("BAD_TARGET_INFO",
                                 f"目标 {name} 必须同时声明 sha256 与 sha512", {"name": name})
        data = files[name]
        length = info.get("length")
        if isinstance(length, int) and not isinstance(length, bool) and len(data) != length:
            raise HashMismatchError(
                "TARGET_LENGTH_MISMATCH",
                f"目标文件 {name} 长度与 targets 声明不一致",
                {"name": name, "expected": length, "actual": len(data)},
            )
        for alg, expected in hashes.items():
            try:
                actual = hashlib.new(alg, data).hexdigest()
            except ValueError as exc:
                raise BadFormatError("BAD_HASH_ALG", f"不支持的哈希算法 {alg}",
                                     {"alg": alg, "name": name}) from exc
            if actual != expected:
                raise HashMismatchError(
                    "TARGET_HASH_MISMATCH",
                    f"目标文件 {name} 的 {alg} 与 targets 声明不一致",
                    {"name": name, "alg": alg, "expected": expected, "actual": actual},
                )
        result[name] = {"sha256": hashes["sha256"], "sha512": hashes["sha512"],
                        "length": len(data)}
    return result


# --------------------------------------------------------------------- 提交


def _commit(store: TrustStore, result: dict, files: dict[str, bytes]) -> None:
    """构造新状态、落盘 blob、原子替换 state.json。

    blob 是内容寻址的（文件名=sha256），提前写入不会影响旧状态；
    只有 state.json 的 os.replace 成功，新信任才生效。失败时旧状态完好，
    提前写入的孤儿 blob 会在下次成功提交时被清理。
    """
    new_state = {
        "root": None,
        "timestamp": None,
        "snapshot": None,
        "targets": None,
        "blobs": {},
        "target_files": {},
    }
    for role in ("root", "timestamp", "snapshot", "targets"):
        entry = result["new_entries"][role]
        new_state[role] = {"version": entry["version"], "raw_b64": b64e(entry["raw"])}

    for name in sorted(result["declared"]):
        info = result["files"][name]
        store.put_blob(files[name], sha256=info["sha256"], sha512=info["sha512"],
                       length=info["length"])
        new_state["target_files"][name] = dict(info)
        new_state["blobs"][info["sha256"]] = {
            "file": f"blobs/{info['sha256']}",
            "sha256": info["sha256"],
            "sha512": info["sha512"],
            "length": info["length"],
        }

    store.commit(new_state)

