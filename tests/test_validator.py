"""验证器的安全属性测试：合法升级、回滚、冻结、混搭、根轮换、原子提交。"""

from __future__ import annotations

import json
import os
from datetime import timedelta

import pytest

from app import metadata as md
from app import repo_tool
from app.errors import (
    BindingError,
    ExpiredMetadataError,
    HashMismatchError,
    NoStateError,
    RollbackError,
    SignatureError,
    StateExistsError,
)
from app.updater import TrustStore, validate_bootstrap

from conftest import (
    Lab,
    drop_signatures,
    replace_signed,
    tamper_signature,
)

V1 = {md.TARGETS: 1, md.SNAPSHOT: 1, md.TIMESTAMP: 1}
V2 = {md.TARGETS: 2, md.SNAPSHOT: 2, md.TIMESTAMP: 2}
V3 = {md.TARGETS: 3, md.SNAPSHOT: 3, md.TIMESTAMP: 3}


@pytest.fixture
def store(tmp_path) -> TrustStore:
    return TrustStore(str(tmp_path / "data"))


@pytest.fixture
def bootstrapped(store: TrustStore, lab: Lab) -> TrustStore:
    store.bootstrap(lab.root_v1(), now=lab.now)
    return store


def _release_v(lab: Lab, keys, versions: dict, payload: bytes | None) -> dict:
    files = {"app.txt": payload} if payload is not None else {}
    return lab.release(keys, versions, files)


# ---------------------------------------------------------------------------
# 引导
# ---------------------------------------------------------------------------


def test_bootstrap_accepts_self_signed_root(store: TrustStore, lab: Lab) -> None:
    result = store.bootstrap(lab.root_v1(), now=lab.now)
    assert result == {"status": "bootstrapped", "root_version": 1}
    assert store.status()["bootstrapped"] is True


def test_bootstrap_rejects_unsigned_root(store: TrustStore, lab: Lab) -> None:
    with pytest.raises(SignatureError):
        store.bootstrap(drop_signatures(lab.root_v1()), now=lab.now)
    assert store.load() is None  # 失败不留状态


def test_bootstrap_rejects_expired_root(store: TrustStore, lab: Lab) -> None:
    old_root = repo_tool.build_root(
        version=1,
        keys=lab.keys_a,
        signers=lab.keys_a.root_keys,
        expires=lab.expires_ago(1),
    )
    with pytest.raises(ExpiredMetadataError):
        validate_bootstrap(old_root, lab.now)


def test_double_bootstrap_rejected(bootstrapped: TrustStore, lab: Lab) -> None:
    with pytest.raises(StateExistsError):
        bootstrapped.bootstrap(lab.root_v1(), now=lab.now)


def test_update_before_bootstrap_rejected(store: TrustStore, lab: Lab) -> None:
    rel = _release_v(lab, lab.keys_a, V1, b"v1")
    with pytest.raises(NoStateError):
        store.apply_update(lab.bundle(rel, {"app.txt": b"v1"}), now=lab.now)


# ---------------------------------------------------------------------------
# 合法升级链 + 恢复
# ---------------------------------------------------------------------------


def test_legit_v1_then_v2_then_v3(
    bootstrapped: TrustStore, lab: Lab
) -> None:
    r1 = _release_v(lab, lab.keys_a, V1, b"app-v1")
    out1 = bootstrapped.apply_update(
        lab.bundle(r1, {"app.txt": b"app-v1"}), now=lab.now
    )
    assert out1["versions"] == {"root": 1, "targets": 1, "snapshot": 1,
                                "timestamp": 1}

    r2 = _release_v(lab, lab.keys_a, V2, b"app-v2")
    out2 = bootstrapped.apply_update(
        lab.bundle(r2, {"app.txt": b"app-v2"}), now=lab.now
    )
    assert out2["versions"]["timestamp"] == 2

    data, meta = bootstrapped.open_target("app.txt")
    assert data == b"app-v2" and meta.length == len(b"app-v2")

    # v3 —— “合法下一版本恢复”的常规形态
    r3 = _release_v(lab, lab.keys_a, V3, b"app-v3")
    out3 = bootstrapped.apply_update(
        lab.bundle(r3, {"app.txt": b"app-v3"}), now=lab.now
    )
    assert out3["versions"]["targets"] == 3


def test_target_download_rehashes_content(
    bootstrapped: TrustStore, lab: Lab, tmp_path
) -> None:
    r1 = _release_v(lab, lab.keys_a, V1, b"hello")
    bootstrapped.apply_update(
        lab.bundle(r1, {"app.txt": b"hello"}), now=lab.now
    )
    data, _ = bootstrapped.open_target("app.txt")
    assert data == b"hello"
    with pytest.raises(Exception):
        bootstrapped.open_target("missing.txt")


# ---------------------------------------------------------------------------
# 回滚攻击：版本号倒退 / 重放
# ---------------------------------------------------------------------------


def test_rollback_timestamp_rejected(
    bootstrapped: TrustStore, lab: Lab
) -> None:
    r1 = _release_v(lab, lab.keys_a, V1, b"app-v1")
    bootstrapped.apply_update(
        lab.bundle(r1, {"app.txt": b"app-v1"}), now=lab.now
    )
    r2 = _release_v(lab, lab.keys_a, V2, b"app-v2")
    bootstrapped.apply_update(
        lab.bundle(r2, {"app.txt": b"app-v2"}), now=lab.now
    )
    # 攻击者重放旧的 v1 整包
    with pytest.raises(RollbackError):
        bootstrapped.apply_update(
            lab.bundle(r1, {"app.txt": b"app-v1"}), now=lab.now
        )
    # 状态仍然是 v2
    assert bootstrapped.load().timestamp.signed["version"] == 2


def test_replay_same_version_rejected(
    bootstrapped: TrustStore, lab: Lab
) -> None:
    r1 = _release_v(lab, lab.keys_a, V1, b"app-v1")
    bootstrapped.apply_update(
        lab.bundle(r1, {"app.txt": b"app-v1"}), now=lab.now
    )
    with pytest.raises(RollbackError):
        bootstrapped.apply_update(
            lab.bundle(r1, {"app.txt": b"app-v1"}), now=lab.now
        )


def test_first_update_must_start_at_v1(
    bootstrapped: TrustStore, lab: Lab
) -> None:
    rel = _release_v(lab, lab.keys_a, V2, b"x")
    with pytest.raises(RollbackError):
        bootstrapped.apply_update(
            lab.bundle(rel, {"app.txt": b"x"}), now=lab.now
        )


# ---------------------------------------------------------------------------
# 冻结攻击：旧 timestamp 过期后重放
# ---------------------------------------------------------------------------


def test_frozen_old_timestamp_rejected(
    bootstrapped: TrustStore, lab: Lab
) -> None:
    # v1 的 timestamp 有效期 30 天
    rel = lab.release(
        lab.keys_a, V1, {"app.txt": b"v1"}, timestamp_days=30
    )
    bootstrapped.apply_update(
        lab.bundle(rel, {"app.txt": b"v1"}), now=lab.now
    )
    # 40 天后攻击者把同一份 v1 再放出来：签名仍合法，但 timestamp 已过期
    with pytest.raises(ExpiredMetadataError):
        bootstrapped.apply_update(
            lab.bundle(rel, {"app.txt": b"v1"}),
            now=lab.now + timedelta(days=40),
        )
    assert bootstrapped.load().timestamp.signed["version"] == 1


def test_fresh_timestamp_after_freeze_recovers(
    bootstrapped: TrustStore, lab: Lab
) -> None:
    rel1 = lab.release(lab.keys_a, V1, {"app.txt": b"v1"}, timestamp_days=30)
    bootstrapped.apply_update(
        lab.bundle(rel1, {"app.txt": b"v1"}), now=lab.now
    )
    # 40 天后：旧包会被过期规则拒绝，而合法下一版本（长有效期）可恢复
    rel2 = lab.release(
        lab.keys_a,
        V2,
        {"app.txt": b"v2"},
        timestamp_days=365,
    )
    out = bootstrapped.apply_update(
        lab.bundle(rel2, {"app.txt": b"v2"}),
        now=lab.now + timedelta(days=40),
    )
    assert out["versions"]["timestamp"] == 2


# ---------------------------------------------------------------------------
# 混搭攻击：跨角色哈希/版本绑定
# ---------------------------------------------------------------------------


def test_mix_match_wrong_snapshot_hash_rejected(
    bootstrapped: TrustStore, lab: Lab
) -> None:
    # 先正常接受 v1
    rel1 = lab.release(lab.keys_a, V1, {"app.txt": b"app-v1"})
    bootstrapped.apply_update(
        lab.bundle(rel1, {"app.txt": b"app-v1"}), now=lab.now
    )
    # 攻击者把历史/未来版本的部件混搭：v2 的 snapshot+targets，
    # 配上重新签发的 timestamp v2 —— 但该 timestamp 绑定的是旧 v1
    # snapshot 的哈希，与随附的 v2 snapshot 字节不符
    files2 = {"app.txt": b"app-v2"}
    rel2 = lab.release(lab.keys_a, V2, files2)
    ts_mixed = repo_tool.build_timestamp(
        version=2,
        snapshot_raw=rel1[md.SNAPSHOT],  # 绑定旧 snapshot
        snapshot_version=1,
        signer=lab.keys_a.timestamp_key,
        expires=lab.expires_in(30),
    )
    mixed = lab.bundle(
        {
            md.TIMESTAMP: ts_mixed,
            md.SNAPSHOT: rel2[md.SNAPSHOT],  # 却塞入新 snapshot
            md.TARGETS: rel2[md.TARGETS],
        },
        files2,
    )
    with pytest.raises(HashMismatchError):
        bootstrapped.apply_update(mixed, now=lab.now)
    assert bootstrapped.load().timestamp.signed["version"] == 1


def test_mix_match_version_crossbind_rejected(
    bootstrapped: TrustStore, lab: Lab
) -> None:
    # timestamp 与 snapshot 哈希一致，但声明的 snapshot.version=5
    # 与 snapshot 自身版本 1 不符（内部矛盾的包）
    rel = lab.release(lab.keys_a, V1, {"app.txt": b"v1"})
    resigned_ts = repo_tool.build_timestamp(
        version=1,
        snapshot_raw=rel[md.SNAPSHOT],
        snapshot_version=5,
        signer=lab.keys_a.timestamp_key,
        expires=lab.expires_in(30),
    )
    mixed = lab.bundle(
        {
            md.TIMESTAMP: resigned_ts,
            md.SNAPSHOT: rel[md.SNAPSHOT],
            md.TARGETS: rel[md.TARGETS],
        },
        {"app.txt": b"v1"},
    )
    with pytest.raises(BindingError):
        bootstrapped.apply_update(mixed, now=lab.now)


def test_mix_match_targets_byte_hash_rejected(
    bootstrapped: TrustStore, lab: Lab
) -> None:
    rel = lab.release(lab.keys_a, V1, {"app.txt": b"v1"})
    # 用一个重新签名的“别的 targets 字节”替换，snapshot 哈希立刻对不上
    other_targets = repo_tool.build_targets(
        version=1,
        files={"app.txt": b"different-content"},
        signer=lab.keys_a.targets_key,
        expires=lab.expires_in(90),
    )
    mixed = lab.bundle(
        {
            md.TIMESTAMP: rel[md.TIMESTAMP],
            md.SNAPSHOT: rel[md.SNAPSHOT],
            md.TARGETS: other_targets,
        },
        {"app.txt": b"different-content"},
    )
    with pytest.raises(HashMismatchError):
        bootstrapped.apply_update(mixed, now=lab.now)


def test_target_file_swap_rejected(
    bootstrapped: TrustStore, lab: Lab
) -> None:
    rel = lab.release(lab.keys_a, V1, {"app.txt": b"legit"})
    # 元数据全部合法且互相绑定，但随附文件被掉包
    with pytest.raises(HashMismatchError):
        bootstrapped.apply_update(
            lab.bundle(rel, {"app.txt": b"EVIL"}), now=lab.now
        )
    with pytest.raises(Exception):
        bootstrapped.open_target("app.txt")  # 未接受任何文件


def test_undeclared_target_file_rejected(
    bootstrapped: TrustStore, lab: Lab
) -> None:
    rel = lab.release(lab.keys_a, V1, {"app.txt": b"v1"})
    bundle = lab.bundle(rel, {"app.txt": b"v1", "extra.bin": b"x"})
    with pytest.raises(BindingError):
        bootstrapped.apply_update(bundle, now=lab.now)


# ---------------------------------------------------------------------------
# 签名
# ---------------------------------------------------------------------------


def test_tampered_signature_rejected(
    bootstrapped: TrustStore, lab: Lab
) -> None:
    rel = lab.release(lab.keys_a, V1, {"app.txt": b"v1"})
    bad = dict(rel)
    bad[md.TIMESTAMP] = tamper_signature(rel[md.TIMESTAMP])
    with pytest.raises(SignatureError):
        bootstrapped.apply_update(
            lab.bundle(bad, {"app.txt": b"v1"}), now=lab.now
        )


def test_unsigned_metadata_rejected(
    bootstrapped: TrustStore, lab: Lab
) -> None:
    rel = lab.release(lab.keys_a, V1, {"app.txt": b"v1"})
    bad = dict(rel)
    bad[md.SNAPSHOT] = drop_signatures(rel[md.SNAPSHOT])
    with pytest.raises(SignatureError):
        bootstrapped.apply_update(
            lab.bundle(bad, {"app.txt": b"v1"}), now=lab.now
        )


def test_metadata_signed_after_tamper_rejected(
    bootstrapped: TrustStore, lab: Lab
) -> None:
    rel = lab.release(lab.keys_a, V1, {"app.txt": b"v1"})
    # 改了 targets 内容但不重新签名 -> 签名失效
    bad = dict(rel)
    bad[md.TARGETS] = replace_signed(
        rel[md.TARGETS],
        lambda s: dict(s, targets={
            "app.txt": {"length": 2,
                        "hashes": {"sha256": "ab" * 32}}
        }),
    )
    with pytest.raises(SignatureError):
        bootstrapped.apply_update(
            lab.bundle(bad, {"app.txt": b"v1"}), now=lab.now
        )


# ---------------------------------------------------------------------------
# 根轮换：合法轮换、伪造、跳号、中断与恢复
# ---------------------------------------------------------------------------


def test_legit_root_rotation_a_to_b(
    bootstrapped: TrustStore, lab: Lab
) -> None:
    # 先接受 A 时代的 v1
    rel1 = lab.release(lab.keys_a, V1, {"app.txt": b"pre-rotation"})
    bootstrapped.apply_update(
        lab.bundle(rel1, {"app.txt": b"pre-rotation"}), now=lab.now
    )

    root2 = lab.root_v2_rotation()
    # 新 root 授权 B 套密钥，因此三段元数据也要由 B 签发，版本继续递增
    rel = lab.release(lab.keys_b, V2, {"app.txt": b"post-rotation"})
    out = bootstrapped.apply_update(
        lab.bundle(rel, {"app.txt": b"post-rotation"}, root=root2),
        now=lab.now,
    )
    assert out["versions"][md.ROOT] == 2

    # 旧 A 密钥签名的更新从此被拒
    old_rel = lab.release(lab.keys_a, V3, {"app.txt": b"signed-by-A"})
    with pytest.raises(SignatureError):
        bootstrapped.apply_update(
            lab.bundle(old_rel, {"app.txt": b"signed-by-A"}), now=lab.now
        )

    # B 签名的合法下一版本可以继续
    rel_b = lab.release(lab.keys_b, V3, {"app.txt": b"signed-by-B"})
    out2 = bootstrapped.apply_update(
        lab.bundle(rel_b, {"app.txt": b"signed-by-B"}), now=lab.now
    )
    assert out2["versions"][md.TIMESTAMP] == 3


def test_forged_root_rotation_rejected(
    bootstrapped: TrustStore, lab: Lab
) -> None:
    # 攻击者只有自己的密钥，伪造一个 root v2
    forged = repo_tool.build_root(
        version=2,
        keys=lab.keys_b,
        signers=lab.keys_b.root_keys,  # 没有旧 root(A) 的授权签名
        expires=lab.expires_in(3650),
    )
    rel = lab.release(lab.keys_b, V2, {"app.txt": b"x"})
    with pytest.raises(SignatureError):
        bootstrapped.apply_update(
            lab.bundle(rel, {"app.txt": b"x"}, root=forged), now=lab.now
        )
    assert bootstrapped.load().root.signed["version"] == 1


def test_root_version_skip_rejected(
    bootstrapped: TrustStore, lab: Lab
) -> None:
    # A+B 双签但版本号直接跳到 v3
    skip = repo_tool.build_root(
        version=3,
        keys=lab.keys_b,
        signers=[*lab.keys_a.root_keys, *lab.keys_b.root_keys],
        expires=lab.expires_in(3650),
    )
    rel = lab.release(lab.keys_b, V2, {"app.txt": b"x"})
    with pytest.raises(RollbackError):
        bootstrapped.apply_update(
            lab.bundle(rel, {"app.txt": b"x"}, root=skip), now=lab.now
        )


def test_root_rollback_v2_to_v1_rejected(
    bootstrapped: TrustStore, lab: Lab
) -> None:
    rel1 = lab.release(lab.keys_a, V1, {"app.txt": b"a1"})
    bootstrapped.apply_update(
        lab.bundle(rel1, {"app.txt": b"a1"}), now=lab.now
    )
    bootstrapped.apply_update(
        lab.bundle(
            lab.release(lab.keys_b, V2, {"app.txt": b"b2"}),
            {"app.txt": b"b2"},
            root=lab.root_v2_rotation(),
        ),
        now=lab.now,
    )
    # 试图把旧的 v1 root 塞回来（即使它签名完全合法）
    rel = lab.release(lab.keys_a, V3, {"app.txt": b"a-again"})
    with pytest.raises(RollbackError):
        bootstrapped.apply_update(
            lab.bundle(rel, {"app.txt": b"a-again"}, root=lab.root_v1()),
            now=lab.now,
        )


def test_root_rotation_interrupt_is_atomic(
    bootstrapped: TrustStore, lab: Lab
) -> None:
    """“根轮换中断”：在提交点前模拟崩溃，状态必须原封不动。"""

    before_path = bootstrapped.state_path
    before = open(before_path, "rb").read()

    root2 = lab.root_v2_rotation()
    rel = lab.release(lab.keys_b, V1, {"app.txt": b"will-not-commit"})
    bundle = lab.bundle(
        rel, {"app.txt": b"will-not-commit"}, root=root2,
        fail_before_commit=True,
    )
    with pytest.raises(RuntimeError):
        bootstrapped.apply_update(bundle, now=lab.now)

    # 状态文件字节完全不变，版本仍是 root v1，且没有三段元数据
    after = open(before_path, "rb").read()
    assert after == before
    state = bootstrapped.load()
    assert state.root.signed["version"] == 1
    assert state.timestamp is None

    # 崩溃后，合法的轮换包仍然可以正常提交（可恢复）
    ok_rel = lab.release(lab.keys_b, V1, {"app.txt": b"after-recovery"})
    out = bootstrapped.apply_update(
        lab.bundle(ok_rel, {"app.txt": b"after-recovery"}, root=root2),
        now=lab.now,
    )
    assert out["versions"][md.ROOT] == 2
    data, _ = bootstrapped.open_target("app.txt")
    assert data == b"after-recovery"


def test_failed_update_leaves_no_tmp_files(
    bootstrapped: TrustStore, lab: Lab
) -> None:
    rel = lab.release(lab.keys_a, V1, {"app.txt": b"legit"})
    with pytest.raises(HashMismatchError):
        bootstrapped.apply_update(
            lab.bundle(rel, {"app.txt": b"EVIL"}), now=lab.now
        )
    leftovers = [
        n for n in os.listdir(bootstrapped.store_dir)
        if n.endswith(".tmp")
    ]
    assert leftovers == []


def test_corrupt_state_file_detected(bootstrapped: TrustStore) -> None:
    with open(bootstrapped.state_path, "wb") as fh:
        fh.write(b"{not json")
    with pytest.raises(json.JSONDecodeError):
        bootstrapped.load()
