"""验证链的基础行为：引导、合法更新、签名/哈希/过期/版本检查。"""

import json
from datetime import datetime, timedelta, timezone

import pytest

from app import crypto_utils, metadata as meta
from app.builder import Repository, bump_all
from app.errors import (
    BadFormatError,
    BadSignatureError,
    ExpiredError,
    HashMismatchError,
)
from app.storage import TrustStore
from app.verifier import _verify_files, apply_update


def _apply(store, bundle, repo):
    return apply_update(store, bundle, bootstrap_root_pub=repo.root_pub_hex())


def test_bootstrap_and_idempotent_resubmit(store, repo):
    summary = _apply(store, repo.bundle(), repo)
    assert summary["accepted"] is True
    assert summary["root_version"] == 1
    assert summary["targets_version"] == 1
    assert summary["root_rotated"] is True
    assert {t["name"] for t in summary["targets"]} == {"app.bin"}

    # 同一包重复提交：幂等，不报错、无角色更新
    again = _apply(store, repo.bundle(), repo)
    assert again["targets_version"] == 1
    assert again["roles_updated"] == []
    assert again["root_rotated"] is False


def test_legitimate_next_version_installs_new_files(store, repo):
    _apply(store, repo.bundle(), repo)
    bump_all(repo, {"app.bin": b"app-version-2", "extra.dat": b"new-file"})
    summary = _apply(store, repo.bundle(), repo)
    assert (summary["targets_version"], summary["snapshot_version"],
            summary["timestamp_version"]) == (2, 2, 2)
    assert sorted(t["name"] for t in summary["targets"]) == ["app.bin", "extra.dat"]

    state = store.load()
    assert set(state["target_files"]) == {"app.bin", "extra.dat"}
    assert store.read_blob(state["target_files"]["app.bin"]["sha256"]) == b"app-version-2"
    # 旧版本文件（app-version-1）的 blob 已被清理
    assert len(list(store.blobs_dir.glob("*"))) == 2


def test_bootstrap_requires_root_v1(tmp_path, repo):
    repo.rotate_root()  # root 变为 v2
    fresh = TrustStore(tmp_path / "fresh")
    fresh.ensure_dirs()
    with pytest.raises(Exception) as ei:
        _apply(fresh, repo.bundle(), repo)
    assert ei.value.code == "ROOT_BOOTSTRAP_VERSION"


def test_bootstrap_rejects_wrong_bootstrap_key(store, repo):
    other = crypto_utils.generate_keypair()[1]
    with pytest.raises(BadSignatureError) as ei:
        apply_update(store, repo.bundle(), bootstrap_root_pub=other)
    assert ei.value.code == "THRESHOLD_NOT_MET"


def test_bad_signature_on_targets(store, repo):
    # 改 targets 内容、重签上层哈希绑定，但 targets 仍携带对旧内容的签名
    bundle = repo.stale_signature_for_modified("targets", lambda s: s["targets"].clear())
    with pytest.raises(BadSignatureError):
        _apply(store, bundle, repo)


def test_target_file_hash_mismatch(store, repo):
    # 与原内容等长，确保先命中哈希错误而不是长度错误
    bundle = repo.tamper_file("app.bin", b"XXXXXXXXXXXXX")
    with pytest.raises(HashMismatchError) as ei:
        _apply(store, bundle, repo)
    assert ei.value.code == "TARGET_HASH_MISMATCH"
    # 验证失败不产生任何信任状态
    assert store.load()["targets"] is None


def test_verify_files_rejects_declared_length_mismatch():
    # 直接测试目标文件层的长度绑定（哈希正确但声明长度错误也必须拒绝）
    data = b"abc"
    declared = {"a.bin": {"length": 99, "hashes": meta.file_hashes(data)}}
    with pytest.raises(HashMismatchError) as ei:
        _verify_files({"a.bin": data}, declared)
    assert ei.value.code == "TARGET_LENGTH_MISMATCH"


def test_expired_timestamp_is_rejected(store, repo):
    repo.set_expiry("timestamp",
                    meta.iso_utc(datetime.now(timezone.utc) - timedelta(seconds=1)))
    repo.publish(dict(repo.files))
    with pytest.raises(ExpiredError) as ei:
        _apply(store, repo.bundle(), repo)
    assert ei.value.code == "EXPIRED"
    assert ei.value.detail["role"] == "timestamp"


def test_expired_root_is_rejected(store, repo):
    repo.set_expiry("root", meta.iso_utc(datetime.now(timezone.utc) - timedelta(days=1)))
    repo._publish_root()
    repo.publish(dict(repo.files))
    with pytest.raises(ExpiredError) as ei:
        _apply(store, repo.bundle(), repo)
    assert ei.value.detail["role"] == "root"


def test_unknown_and_missing_target_files(store, repo):
    extra = repo.bundle()
    extra.files["rogue.bin"] = b"not-declared"
    with pytest.raises(BadFormatError) as ei:
        _apply(store, extra, repo)
    assert ei.value.code == "UNKNOWN_TARGET_FILE"

    missing = repo.bundle()
    missing.files = {}
    with pytest.raises(BadFormatError) as ei:
        _apply(store, missing, repo)
    assert ei.value.code == "MISSING_TARGET_FILE"


def test_threshold_two_signed_by_only_one_rejected(tmp_path):
    repo = Repository.create()
    # 给 timestamp 配第二把密钥、threshold=2，重建尚未引导的 root v1
    repo.roles["timestamp"].priv_hexes.append(crypto_utils.generate_keypair()[0])
    repo.roles["timestamp"].threshold = 2
    repo._publish_root()
    repo.publish(dict(repo.files))
    # 用单把（合法）密钥重签 timestamp —— 签名都合法，但数量不足
    env = json.loads(repo.latest["timestamp"])
    repo.latest["timestamp"] = meta.canonical(meta.sign_signed(
        env["signed"], [repo.roles["timestamp"].priv_hexes[0]]))

    store = TrustStore(tmp_path / "data")
    store.ensure_dirs()
    with pytest.raises(BadSignatureError) as ei:
        _apply(store, repo.bundle(), repo)
    assert ei.value.code == "THRESHOLD_NOT_MET"
    assert ei.value.detail == {"role": "timestamp", "required": 2, "found": 1}
