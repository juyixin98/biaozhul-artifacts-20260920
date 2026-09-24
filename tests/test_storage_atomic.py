"""原子提交：失败不留半成品状态；blob 内容寻址与孤儿清理。"""

import json

import pytest

from app.builder import Repository, bump_all
from app.errors import HashMismatchError
from app.storage import TrustStore
from app.verifier import apply_update


def test_failed_update_leaves_state_untouched(tmp_path, repo):
    store = TrustStore(tmp_path / "data")
    store.ensure_dirs()
    apply_update(store, repo.bundle(), bootstrap_root_pub=repo.root_pub_hex())
    before = store.state_path.read_text()
    state_before = store.load()

    bump_all(repo, {"app.bin": b"app-version-2"})
    attack = repo.tamper_file("app.bin", b"evil")
    with pytest.raises(HashMismatchError):
        apply_update(store, attack, bootstrap_root_pub=repo.root_pub_hex())

    # state.json 与被信任内容完全不变
    assert store.state_path.read_text() == before
    assert store.load() == state_before
    # 攻击文件没有进入 blob 存储
    assert len(list(store.blobs_dir.glob("*"))) == 1


def test_successful_update_replaces_state_atomically(tmp_path, repo):
    store = TrustStore(tmp_path / "data")
    store.ensure_dirs()
    apply_update(store, repo.bundle(), bootstrap_root_pub=repo.root_pub_hex())
    v1_target = store.load()["targets"]["version"]

    bump_all(repo, {"app.bin": b"app-version-2"})
    apply_update(store, repo.bundle(), bootstrap_root_pub=repo.root_pub_hex())

    state = store.load()
    assert state["targets"]["version"] == v1_target + 1
    # state.json 始终是合法 JSON（不存在 .tmp 残留）
    assert not list(store.data_dir.glob(".state.*.tmp"))
    assert not list(store.blobs_dir.glob(".blob.*.tmp"))
    # 只有当前目标文件的 blob 留存
    blobs = list(store.blobs_dir.glob("*"))
    assert len(blobs) == 1
    assert blobs[0].read_bytes() == b"app-version-2"


def test_blob_is_content_addressable_and_deduped(tmp_path):
    repo = Repository.create()
    store = TrustStore(tmp_path / "data")
    store.ensure_dirs()
    # 两个不同名称、内容相同的目标应共享一个 blob
    repo.publish({"a.bin": b"same", "b.bin": b"same"})
    apply_update(store, repo.bundle(), bootstrap_root_pub=repo.root_pub_hex())
    state = store.load()
    assert len(state["blobs"]) == 1
    assert state["target_files"]["a.bin"]["sha256"] == \
        state["target_files"]["b.bin"]["sha256"]


def test_state_file_is_fsynced_json_with_no_temp_leftovers(tmp_path, repo):
    store = TrustStore(tmp_path / "data")
    store.ensure_dirs()
    apply_update(store, repo.bundle(), bootstrap_root_pub=repo.root_pub_hex())
    data = json.loads(store.state_path.read_text())
    for role in ("root", "timestamp", "snapshot", "targets"):
        assert data[role]["version"] >= 1
        assert isinstance(data[role]["raw_b64"], str)


def test_reset_clears_everything(tmp_path, repo):
    store = TrustStore(tmp_path / "data")
    store.ensure_dirs()
    apply_update(store, repo.bundle(), bootstrap_root_pub=repo.root_pub_hex())
    store.reset()
    state = store.load()
    assert state["root"] is None
    assert state["blobs"] == {}
    assert list(store.blobs_dir.glob("*")) == []
