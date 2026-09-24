"""验收测试：三大攻击场景 + 合法下一版本恢复。

1. 混搭历史文件（mix-and-match / confused deputy）；
2. 冻结旧时间戳（freeze / rollback replay）；
3. 根轮换中断（跳过版本、旧/新密钥非法签名）。
所有攻击必须被拒绝，且随后的合法下一版本必须能正常恢复更新。
"""

import json
from datetime import datetime, timedelta, timezone

from app import crypto_utils, metadata as meta
from app.builder import RoleKeys, bump_all
from app.errors import (
    BadSignatureError,
    HashMismatchError,
    InterferenceError,
    RollbackError,
    VersionGapError,
)
from app.storage import TrustStore
from app.verifier import apply_update


def _apply(store, bundle, repo):
    return apply_update(store, bundle, bootstrap_root_pub=repo.root_pub_hex())


def _versions(store):
    state = store.load()
    return {r: (None if state[r] is None else state[r]["version"])
            for r in ("root", "timestamp", "snapshot", "targets")}


# ---------------------------------------------------------------------------
# 场景 1：混搭历史文件
# ---------------------------------------------------------------------------

def test_mix_historical_files_is_rejected_then_recovers(tmp_path, repo):
    store = TrustStore(tmp_path / "data")
    store.ensure_dirs()
    _apply(store, repo.bundle(), repo)
    v1_bundle = repo.bundle()

    # 发布 v2：时间戳/快照/目标全部推进
    bump_all(repo, {"app.bin": b"app-version-2"})
    assert _apply(store, repo.bundle(), repo)["targets_version"] == 2

    # 攻击者把当前时间戳与上一代 snapshot/targets/文件混搭
    mixed = repo.mix_historical(v1_bundle)
    try:
        _apply(store, mixed, repo)
        assert False, "混搭包必须被拒绝"
    except HashMismatchError as exc:
        # timestamp 仍声明当前 snapshot 的哈希，旧 snapshot 对不上
        assert exc.code == "HASH_MISMATCH"
    except (RollbackError, InterferenceError):
        # 或先命中本地版本回滚检查 —— 两者都属于拒绝，接受其一
        pass

    # 本地状态停留在 v2，未被污染
    assert _versions(store) == {"root": 1, "timestamp": 2, "snapshot": 2, "targets": 2}

    # 合法的下一版本 v3 必须能正常恢复
    bump_all(repo, {"app.bin": b"app-version-3"})
    summary = _apply(store, repo.bundle(), repo)
    assert summary["targets_version"] == 3
    state = store.load()
    assert store.read_blob(state["target_files"]["app.bin"]["sha256"]) == b"app-version-3"


# ---------------------------------------------------------------------------
# 场景 2：冻结旧时间戳
# ---------------------------------------------------------------------------

def test_frozen_old_timestamp_is_rejected_then_recovers(tmp_path, repo):
    store = TrustStore(tmp_path / "data")
    store.ensure_dirs()
    _apply(store, repo.bundle(), repo)
    v1_bundle = repo.bundle()

    bump_all(repo, {"app.bin": b"app-version-2"})
    _apply(store, repo.bundle(), repo)

    # 攻击者重放 v1 的时间戳（版本 1 < 已信任的 2）
    frozen = repo.freeze_old_timestamp(v1_bundle)
    try:
        _apply(store, frozen, repo)
        assert False, "冻结旧时间戳必须被拒绝"
    except RollbackError as exc:
        assert exc.code == "TIMESTAMP_ROLLBACK"
    except HashMismatchError:
        # 旧时间戳绑定旧 snapshot 哈希，与包内新 snapshot 不一致时也可能先在此断裂
        pass

    assert _versions(store)["timestamp"] == 2

    # 合法时间戳 v3 恢复
    bump_all(repo, {"app.bin": b"app-version-3"})
    summary = _apply(store, repo.bundle(), repo)
    assert summary["timestamp_version"] == 3
    assert summary["accepted"] is True


def test_frozen_expired_timestamp_also_rejected(tmp_path, repo):
    store = TrustStore(tmp_path / "data")
    store.ensure_dirs()
    _apply(store, repo.bundle(), repo)
    # 攻击者拿到一张尚未过期但将很快过期的时间戳；过期后即使重放也必须拒绝
    repo.set_expiry("timestamp",
                    meta.iso_utc(datetime.now(timezone.utc) + timedelta(days=3650)))
    repo.publish(dict(repo.files))
    _apply(store, repo.bundle(include_root=False), repo)
    old_ts = repo.bundle(include_root=False).timestamp

    future_now = datetime.now(timezone.utc) + timedelta(days=4000)
    # 直接把旧时间戳塞进当前包，并以“未来时间”模拟冻结到期
    attack = repo.bundle(include_root=False)
    attack.timestamp = old_ts
    try:
        apply_update(store, attack, bootstrap_root_pub=repo.root_pub_hex(), now=future_now)
        assert False
    except Exception as ei:
        assert ei.code in ("EXPIRED", "TIMESTAMP_ROLLBACK")


# ---------------------------------------------------------------------------
# 场景 3：根轮换
# ---------------------------------------------------------------------------

def test_legitimate_root_rotation_and_recovery(tmp_path, repo):
    store = TrustStore(tmp_path / "data")
    store.ensure_dirs()
    _apply(store, repo.bundle(), repo)

    # 合法轮换：换全套新密钥，由旧 root 密钥签名 root v2
    new_roles = {r: RoleKeys([crypto_utils.generate_keypair()[0]])
                 for r in ("root", "targets", "snapshot", "timestamp")}
    repo.rotate_root(new_roles)
    summary = _apply(store, repo.bundle(), repo)
    assert summary["root_version"] == 2
    assert summary["root_rotated"] is True

    # 轮换后旧引导密钥不再能为 root 背书；新链路正常工作
    bump_all(repo, {"app.bin": b"app-version-2"})
    summary = _apply(store, repo.bundle(include_root=False), repo)
    assert summary["root_version"] == 2
    assert summary["targets_version"] == 2


def test_root_rotation_gap_is_rejected(tmp_path, repo):
    store = TrustStore(tmp_path / "data")
    store.ensure_dirs()
    _apply(store, repo.bundle(), repo)

    # 先合法轮换到 v2，再伪造一个 v4（跳过 v3）
    new_roles = {r: RoleKeys([crypto_utils.generate_keypair()[0]])
                 for r in ("root", "targets", "snapshot", "timestamp")}
    repo.rotate_root(new_roles)
    _apply(store, repo.bundle(), repo)

    repo.root_version = 4
    bad_raw = repo._publish_root()
    attack = repo.bundle(override={"root": bad_raw})
    try:
        _apply(store, attack, repo)
        assert False, "root 版本跳跃必须被拒绝"
    except VersionGapError as ei:
        assert ei.code == "ROOT_VERSION_GAP"
        assert ei.detail == {"trusted": 2, "received": 4}

    # 合法的 v3 恢复：把版本设回 3，用当前（v2）root 密钥签名
    repo.root_version = 3
    good_v3 = repo._publish_root()
    repo.publish(dict(repo.files))
    summary = _apply(store, repo.bundle(override={"root": good_v3}), repo)
    assert summary["root_version"] == 3


def test_root_rotation_signed_by_new_key_only_is_rejected(tmp_path, repo):
    """轮换中断：新 root 只由新密钥自签（旧 root 未授权），随后合法 v2 可恢复。"""
    store = TrustStore(tmp_path / "data")
    store.ensure_dirs()
    _apply(store, repo.bundle(), repo)

    # 攻击者自制全套密钥，并用新密钥自签 v2 root（不修改 repo 自身状态）
    attacker_roles = {r: RoleKeys([crypto_utils.generate_keypair()[0]])
                      for r in ("root", "targets", "snapshot", "timestamp")}
    forged_v2 = repo.build_root_envelope(
        attacker_roles, 2, attacker_roles["root"].priv_hexes)
    attack = repo.bundle(override={"root": forged_v2})
    try:
        _apply(store, attack, repo)
        assert False, "未经旧 root 授权的轮换必须被拒绝"
    except BadSignatureError as ei:
        assert ei.code == "THRESHOLD_NOT_MET"

    # 本地仍停留在 v1
    assert _versions(store)["root"] == 1

    # 合法的 v2：用旧 root 密钥签名、切换到新角色集合 —— 必须能正常恢复
    new_roles = {r: RoleKeys([crypto_utils.generate_keypair()[0]])
                 for r in ("root", "targets", "snapshot", "timestamp")}
    repo.rotate_root(new_roles)
    summary = _apply(store, repo.bundle(), repo)
    assert summary["root_version"] == 2
    assert summary["root_rotated"] is True

    # 轮换后继续用新链路发布下一版本
    bump_all(repo, {"app.bin": b"app-version-2"})
    summary = _apply(store, repo.bundle(include_root=False), repo)
    assert summary["targets_version"] == 2


def test_root_rollback_v2_to_v1_is_rejected(tmp_path, repo):
    store = TrustStore(tmp_path / "data")
    store.ensure_dirs()
    _apply(store, repo.bundle(), repo)
    v1_root = repo.latest["root"]
    new_roles = {r: RoleKeys([crypto_utils.generate_keypair()[0]])
                 for r in ("root", "targets", "snapshot", "timestamp")}
    repo.rotate_root(new_roles)
    _apply(store, repo.bundle(), repo)

    # 攻击者把 root 字段换回旧 v1
    attack = repo.bundle(override={"root": v1_root})
    try:
        _apply(store, attack, repo)
        assert False, "root 回滚必须被拒绝"
    except RollbackError as ei:
        assert ei.code == "ROOT_ROLLBACK"
    assert _versions(store)["root"] == 2


def test_snapshot_rollback_is_rejected(tmp_path, repo):
    store = TrustStore(tmp_path / "data")
    store.ensure_dirs()
    _apply(store, repo.bundle(), repo)
    v1_snapshot = repo.latest["snapshot"]
    bump_all(repo, {"app.bin": b"app-version-2"})
    _apply(store, repo.bundle(), repo)

    attack = repo.bundle(override={"snapshot": v1_snapshot})
    try:
        _apply(store, attack, repo)
        assert False, "snapshot 回滚必须被拒绝"
    except (RollbackError, HashMismatchError) as ei:
        assert getattr(ei, "code", "") in ("SNAPSHOT_ROLLBACK", "HASH_MISMATCH")

    assert _versions(store)["snapshot"] == 2


def test_replay_previous_full_bundle_is_rejected(tmp_path, repo):
    store = TrustStore(tmp_path / "data")
    store.ensure_dirs()
    _apply(store, repo.bundle(), repo)
    v1 = repo.bundle()
    bump_all(repo, {"app.bin": b"app-version-2"})
    _apply(store, repo.bundle(), repo)

    # 整包重放 v1（所有签名当时都合法）——必须被 timestamp 版本检查拒绝
    try:
        _apply(store, v1, repo)
        assert False, "旧整包重放必须被拒绝"
    except RollbackError as ei:
        assert ei.code == "TIMESTAMP_ROLLBACK"


def test_after_rotation_old_key_signed_same_version_metadata_rejected(tmp_path, repo):
    """根轮换后，用*旧*角色密钥签同版本元数据必须失败（新 root 不再授权它）。"""
    store = TrustStore(tmp_path / "data")
    store.ensure_dirs()
    _apply(store, repo.bundle(), repo)
    old_timestamp_priv = repo.roles["timestamp"].priv_hexes[0]
    old_ts_env = json.loads(repo.latest["timestamp"].decode("utf-8"))

    # 合法轮换到 root v2（timestamp 密钥随之更换、同版本重签）
    new_roles = {r: RoleKeys([crypto_utils.generate_keypair()[0]])
                 for r in ("root", "targets", "snapshot", "timestamp")}
    repo.rotate_root(new_roles)
    _apply(store, repo.bundle(), repo)

    # 攻击者用轮换前的旧 timestamp 私钥重签同版本 timestamp（内容也可微调）
    forged_ts = meta.canonical(meta.sign_signed(old_ts_env["signed"], [old_timestamp_priv]))
    attack = repo.bundle(override={"timestamp": forged_ts})
    try:
        _apply(store, attack, repo)
        assert False, "旧密钥在轮换后签发的元数据必须被拒绝"
    except BadSignatureError as ei:
        assert ei.code == "THRESHOLD_NOT_MET"
    assert _versions(store)["timestamp"] == 1


def test_same_version_timestamp_with_tampered_content_rejected(tmp_path, repo):
    """同版本 timestamp 内容被改且只带旧签名 -> 签名失败。"""
    store = TrustStore(tmp_path / "data")
    store.ensure_dirs()
    _apply(store, repo.bundle(), repo)
    # timestamp 是信任链最顶端，无法重签上层绑定：改内容必然签名失效
    bundle = repo.tamper_metadata_unsigned(
        "timestamp",
        lambda s: s["meta"]["snapshot"].__setitem__("version", 99))
    try:
        _apply(store, bundle, repo)
        assert False, "同版本 timestamp 内容篡改必须被拒绝"
    except BadSignatureError as ei:
        assert ei.code == "BAD_SIGNATURE"
    assert _versions(store)["timestamp"] == 1


def test_snapshot_modified_without_timestamp_rebinding_is_rejected(tmp_path, repo):
    """直接篡改 snapshot 内容但 timestamp 仍绑定旧哈希 -> 签名或哈希链断裂。"""
    store = TrustStore(tmp_path / "data")
    store.ensure_dirs()
    _apply(store, repo.bundle(), repo)
    # 改 snapshot signed、保留旧签名；timestamp 未重新绑定其哈希
    attack = repo.tamper_metadata_unsigned(
        "snapshot", lambda s: s["meta"]["targets.json"].__setitem__("version", 99))
    try:
        _apply(store, attack, repo)
        assert False, "篡改 snapshot 必须被拒绝"
    except (HashMismatchError, BadSignatureError) as ei:
        assert ei.code in ("HASH_MISMATCH", "LENGTH_MISMATCH", "BAD_SIGNATURE")
