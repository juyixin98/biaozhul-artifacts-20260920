"""密钥原语与信任库测试。"""

from pathlib import Path

import pytest

from sam.errors import KeyStoreError, SignatureError, UnknownKeyError
from sam.keys import (
    TrustStore,
    generate_private_key,
    is_valid_key_id,
    load_private_key,
    load_public_key_pem,
    private_key_to_pem,
    public_key_id,
    public_key_to_pem,
    sign,
    verify_signature,
)


def test_keygen_sign_verify_roundtrip():
    sk = generate_private_key()
    vk = sk.public_key()
    msg = b"canonical manifest bytes"
    sig = sign(sk, msg)
    verify_signature(vk, sig, msg)  # 不抛异常即通过


def test_tampered_signature_fails():
    sk = generate_private_key()
    sig = bytearray(sign(sk, b"hello"))
    sig[0] ^= 0xFF
    with pytest.raises(SignatureError):
        verify_signature(sk.public_key(), bytes(sig), b"hello")


def test_wrong_message_fails():
    sk = generate_private_key()
    sig = sign(sk, b"hello")
    with pytest.raises(SignatureError):
        verify_signature(sk.public_key(), sig, b"hellp")


def test_key_id_deterministic_and_unique():
    sk1, sk2 = generate_private_key(), generate_private_key()
    id1, id2 = public_key_id(sk1.public_key()), public_key_id(sk2.public_key())
    assert is_valid_key_id(id1) and is_valid_key_id(id2)
    assert id1 != id2
    # 同一把公钥反复导出, ID 不变
    assert public_key_id(sk1.public_key()) == id1
    # DER 再导入也不变
    reloaded = load_public_key_pem(public_key_to_pem(sk1.public_key()))
    assert public_key_id(reloaded) == id1


def test_trust_store_dir_and_unknown_key(tmp_path: Path):
    tdir = tmp_path / "trust"
    tdir.mkdir()
    sk = generate_private_key()
    (tdir / "owner.pem").write_bytes(public_key_to_pem(sk.public_key()))

    store = TrustStore.load(tdir)
    kid = public_key_id(sk.public_key())
    assert store.get(kid).public_key is not None

    with pytest.raises(UnknownKeyError):
        store.get("0" * 64)
    with pytest.raises(UnknownKeyError):
        store.get("not-hex")


def test_trust_store_single_pem(tmp_path: Path):
    sk = generate_private_key()
    p = tmp_path / "one.pem"
    p.write_bytes(public_key_to_pem(sk.public_key()))
    store = TrustStore.load(p)
    assert public_key_id(sk.public_key()) in store.key_ids()


def test_trust_store_rejects_garbage_and_missing(tmp_path: Path):
    (tmp_path / "bad.pem").write_text("not a key")
    with pytest.raises(KeyStoreError):
        TrustStore.load(tmp_path / "bad.pem")
    with pytest.raises(KeyStoreError):
        TrustStore.load(tmp_path / "nope")
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(KeyStoreError):
        TrustStore.load(empty)


def test_trust_store_duplicate_key_rejected(tmp_path: Path):
    sk = generate_private_key()
    tdir = tmp_path / "trust"
    tdir.mkdir()
    (tdir / "a.pem").write_bytes(public_key_to_pem(sk.public_key()))
    (tdir / "b.pem").write_bytes(public_key_to_pem(sk.public_key()))
    with pytest.raises(KeyStoreError, match="重复"):
        TrustStore.load(tdir)


def test_load_private_key_roundtrip(tmp_path: Path):
    sk = generate_private_key()
    p = tmp_path / "k.pem"
    p.write_bytes(private_key_to_pem(sk))
    loaded = load_private_key(p)
    assert public_key_id(loaded.public_key()) == public_key_id(sk.public_key())
