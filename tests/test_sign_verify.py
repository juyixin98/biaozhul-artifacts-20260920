"""端到端签名/验证测试 —— 覆盖验收要求的全部攻击/故障场景。"""

import json
from pathlib import Path

import pytest

from sam import canonical
from sam.keys import (
    TrustStore,
    generate_private_key,
    public_key_id,
    public_key_to_pem,
)
from sam.sign import sign_artifact
from sam.verify import (
    P_DIGEST_MISMATCH,
    P_ENVELOPE_MALFORMED,
    P_EXTRA_FILE,
    P_FILE_MISSING,
    P_SIGNATURE,
    P_UNKNOWN_KEY,
    P_UNSAFE_PATH,
    verify,
)


@pytest.fixture
def owner():
    return generate_private_key()


@pytest.fixture
def artifact(tmp_path: Path) -> Path:
    root = tmp_path / "artifact"
    (root / "bin").mkdir(parents=True)
    (root / "bin" / "app.sh").write_text("#!/bin/sh\necho hello\n")
    (root / "config").mkdir()
    (root / "config" / "settings.json").write_text('{"level": 3}\n')
    (root / "README").write_text("demo artifact\n")
    return root


@pytest.fixture
def trust_dir(tmp_path: Path, owner) -> Path:
    tdir = tmp_path / "trust"
    tdir.mkdir()
    (tdir / "owner.pem").write_bytes(public_key_to_pem(owner.public_key()))
    return tdir


def _envelope_bytes(artifact: Path, owner, entrypoint=None) -> bytes:
    envelope = sign_artifact(artifact, owner, entrypoint=entrypoint)
    return canonical.pretty_json(envelope)


def test_happy_path(artifact, owner, trust_dir):
    env = _envelope_bytes(artifact, owner)
    store = TrustStore.load(trust_dir)
    report = verify(artifact, env, store)
    assert report.ok, [p.message for p in report.problems]
    assert report.file_count == 3
    assert report.key_id == public_key_id(owner.public_key())


# ---- 验收点 1: 字段重排不影响验证 ----


def test_field_reordering_still_verifies(artifact, owner, trust_dir):
    envelope = sign_artifact(artifact, owner)
    raw = canonical.canonical_bytes(envelope)  # 这是紧凑形式
    parsed = json.loads(raw)

    # 重排封套顶层键 (对象键序对规范化不可见)
    reordered = {
        "signature": parsed["signature"],
        "manifest": parsed["manifest"],
        "algorithm": parsed["algorithm"],
        "sam_version": parsed["sam_version"],
    }
    # 重排 manifest *对象键*; files 数组保持原顺序 (数组是有序的)
    m = reordered["manifest"]
    reordered["manifest"] = {
        "entrypoint": m["entrypoint"],
        "created_at": m["created_at"],
        "key_id": m["key_id"],
        "algorithm": m["algorithm"],
        "sam_version": m["sam_version"],
        "files": m["files"],
    }
    blob = json.dumps(reordered, indent=5).encode()  # 任意空白/缩进
    report = verify(artifact, blob, TrustStore.load(trust_dir))
    assert report.ok, [p.message for p in report.problems]


def test_file_array_reorder_breaks_signature(artifact, owner, trust_dir):
    # 数组是有序的: 交换 files 中两个条目的位置必须使签名失效
    envelope = sign_artifact(artifact, owner)
    m = envelope["manifest"]
    m["files"] = [m["files"][1], m["files"][0]] + m["files"][2:]
    blob = canonical.pretty_json(envelope)
    report = verify(artifact, blob, TrustStore.load(trust_dir))
    assert any(p.code == P_SIGNATURE for p in report.problems)


# ---- 验收点 2: 重复 JSON 键拒绝 ----


def test_duplicate_json_keys_rejected(artifact, owner, trust_dir):
    env = _envelope_bytes(artifact, owner)
    text = env.decode()
    # 在 manifest 里制造重复键 (signature 重复同样拒绝)
    tampered = text.replace(
        '"sam_version": 1', '"sam_version": 1, "sam_version": 1', 1
    )
    report = verify(artifact, tampered.encode(), TrustStore.load(trust_dir))
    assert not report.ok
    assert report.problems[0].code == P_ENVELOPE_MALFORMED
    assert "重复" in report.problems[0].message


# ---- 验收点 3: 路径穿越拒绝 ----


def test_path_traversal_in_manifest_rejected(artifact, owner, trust_dir):
    envelope = sign_artifact(artifact, owner)
    envelope["manifest"]["files"][0]["path"] = "../../etc/passwd"
    # 用攻击者私钥重新签名也没用 —— 路径检查独立于签名
    attacker = generate_private_key()
    from sam.manifest import make_envelope, manifest_signing_bytes
    from sam.keys import sign

    envelope["manifest"]["key_id"] = public_key_id(attacker.public_key())
    # 但攻击者不在信任库; 我们也单独验证: 路径问题与密钥问题同时报出
    envelope = make_envelope(
        envelope["manifest"], sign(attacker, manifest_signing_bytes(envelope["manifest"]))
    )
    blob = canonical.pretty_json(envelope)
    report = verify(artifact, blob, TrustStore.load(trust_dir))
    codes = {p.code for p in report.problems}
    assert P_UNSAFE_PATH in codes
    assert P_UNKNOWN_KEY in codes  # 纵深防御: 多重问题都会暴露


def test_absolute_path_rejected(artifact, owner, trust_dir):
    envelope = sign_artifact(artifact, owner)
    envelope["manifest"]["files"][0]["path"] = "/etc/passwd"
    blob = canonical.pretty_json(envelope)
    report = verify(artifact, blob, TrustStore.load(trust_dir))
    assert P_UNSAFE_PATH in {p.code for p in report.problems}


# ---- 验收点 4: 文件缺失 ----


def test_missing_file_fails(artifact, owner, trust_dir):
    env = _envelope_bytes(artifact, owner)
    (artifact / "README").unlink()
    report = verify(artifact, env, TrustStore.load(trust_dir))
    assert not report.ok
    assert any(p.code == P_FILE_MISSING and p.path == "README" for p in report.problems)


# ---- 验收点 5: 未知密钥 ----


def test_unknown_key_fails(artifact, trust_dir):
    stranger = generate_private_key()
    env = _envelope_bytes(artifact, stranger)
    report = verify(artifact, env, TrustStore.load(trust_dir))
    assert not report.ok
    assert any(p.code == P_UNKNOWN_KEY for p in report.problems)


# ---- 摘要不匹配: 制品内容被改 ----


def test_content_tamper_digest_mismatch(artifact, owner, trust_dir):
    env = _envelope_bytes(artifact, owner)
    (artifact / "README").write_text("tampered content\n")
    report = verify(artifact, env, TrustStore.load(trust_dir))
    codes = {p.code for p in report.problems}
    assert P_DIGEST_MISMATCH in codes


def test_size_tamper_reported(artifact, owner, trust_dir):
    env = _envelope_bytes(artifact, owner)
    (artifact / "README").write_text("x")  # 长度变化
    report = verify(artifact, env, TrustStore.load(trust_dir))
    from sam.verify import P_SIZE_MISMATCH

    codes = {p.code for p in report.problems}
    # 长度不同 -> size 与 digest 都报
    assert P_SIZE_MISMATCH in codes and P_DIGEST_MISMATCH in codes


# ---- 签名被伪造/翻转 ----


def test_signature_bitflip_fails(artifact, owner, trust_dir):
    envelope = sign_artifact(artifact, owner)
    import base64

    sig = bytearray(base64.b64decode(envelope["signature"]))
    sig[-1] ^= 0x01
    envelope["signature"] = base64.b64encode(bytes(sig)).decode()
    blob = canonical.pretty_json(envelope)
    report = verify(artifact, blob, TrustStore.load(trust_dir))
    assert any(p.code == P_SIGNATURE for p in report.problems)


def test_manifest_tampered_after_signing(artifact, owner, trust_dir):
    envelope = sign_artifact(artifact, owner)
    envelope["manifest"]["created_at"] = "1999-01-01T00:00:00Z"
    blob = canonical.pretty_json(envelope)
    report = verify(artifact, blob, TrustStore.load(trust_dir))
    assert any(p.code == P_SIGNATURE for p in report.problems)


def test_algorithm_tamper_fails(artifact, owner, trust_dir):
    envelope = sign_artifact(artifact, owner)
    envelope["algorithm"] = "rsa-pss-sha256"
    blob = canonical.pretty_json(envelope)
    report = verify(artifact, blob, TrustStore.load(trust_dir))
    assert not report.ok


# ---- 清单外文件 ----


def test_extra_file_detected(artifact, owner, trust_dir):
    env = _envelope_bytes(artifact, owner)
    (artifact / "malware.sh").write_text("echo pwned")
    report = verify(artifact, env, TrustStore.load(trust_dir))
    assert any(
        p.code == P_EXTRA_FILE and p.path == "malware.sh" for p in report.problems
    )
    # 显式放宽时只告警不失败
    report2 = verify(
        artifact, env, TrustStore.load(trust_dir), strict_extra=False
    )
    assert report2.ok


# ---- entrypoint 规则 ----


def test_entrypoint_happy_and_traversal(artifact, owner, trust_dir):
    env = _envelope_bytes(artifact, owner, entrypoint="bin/app.sh")
    report = verify(artifact, env, TrustStore.load(trust_dir))
    assert report.ok and report.entrypoint == "bin/app.sh"

    # 在*已签名封套*上把 entrypoint 改成逃逸路径 -> 词法拒绝且签名失效
    envelope = sign_artifact(artifact, owner, entrypoint="bin/app.sh")
    envelope["manifest"]["entrypoint"] = "../escape"
    blob = canonical.pretty_json(envelope)
    report = verify(artifact, blob, TrustStore.load(trust_dir))
    assert not report.ok
    from sam.verify import P_ENTRYPOINT

    codes = {p.code for p in report.problems}
    assert P_ENTRYPOINT in codes or P_UNSAFE_PATH in codes


# ---- 畸形封套 ----


def test_malformed_json(artifact, trust_dir):
    report = verify(artifact, b"{not json", TrustStore.load(trust_dir))
    assert not report.ok
    assert report.problems[0].code == P_ENVELOPE_MALFORMED


def test_nan_in_envelope_rejected(artifact, owner, trust_dir):
    env = _envelope_bytes(artifact, owner).replace(b"1", b"NaN", 1)
    report = verify(artifact, env, TrustStore.load(trust_dir))
    assert not report.ok
