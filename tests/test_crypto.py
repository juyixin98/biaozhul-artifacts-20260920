"""密码学原语：使用公开标准向量做真实计算验证。"""

import pytest

from app.crypto import (
    base58_decode,
    base58_encode,
    is_valid_eip55,
    keccak256,
    sha256,
    swarm_bmt_single,
    to_cidv0,
    to_eip55,
)

# Keccak-256("") —— 以太坊真实使用的 Keccak（非 NIST SHA3）
KECCAK_EMPTY = "c5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470"
SHA256_EMPTY = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
SHA256_ABC = "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"


def test_sha256_standard_vectors():
    assert sha256(b"").hex() == SHA256_EMPTY
    assert sha256(b"abc").hex() == SHA256_ABC


def test_keccak256_is_ethereum_keccak_not_sha3():
    assert keccak256(b"").hex() == KECCAK_EMPTY
    # NIST SHA3-256("") 与 Keccak 不同，证明用的是正确的填充
    import hashlib

    assert hashlib.sha3_256(b"").hexdigest() != KECCAK_EMPTY
    assert keccak256(b"abc").hex() == "4e03657aea45a94fc7d47ba826c8d667c0d1e6e33a64a036ec44f58fa12d6c45"


EIP55_VECTORS = [
    "0x52908400098527886E0F7030069857D2E4169EE7",
    "0x8617E340B3D01FA5F11F306F4090FD50E238070D",
    "0xde709f2102306220921060314715629080e2fb77",
    "0x27b1fdb04752bbc536007a920d24acb045561c26",
    "0x5aAeb6053F3E94C9b9A09f33669435E7Ef1BeAed",
    "0xfB6916095ca1df60bB79Ce92cE3Ea74c37c5d359",
    "0xdbF03B407c01E7cD3CBea99509d93f8DDDC8C6FB",
    "0xD1220A0cf47c7B9Be7A2E6BA89F429762e7b9aDb",
]


def test_eip55_official_vectors_all_valid():
    for v in EIP55_VECTORS:
        assert is_valid_eip55(v), v


def test_eip55_roundtrip_and_bad_checksum_rejected():
    raw = bytes.fromhex("5aaeb6053f3e94c9b9a09f33669435e7ef1beaed")
    assert to_eip55(raw) == EIP55_VECTORS[4]
    # 只翻转一个字母的大小写 -> 校验失败
    assert not is_valid_eip55("0x5AAeb6053F3E94C9b9A09f33669435E7Ef1BeAed")
    assert not is_valid_eip55("0x1234")
    assert not is_valid_eip55("not-an-address")


def test_base58_roundtrip_and_cidv0_structure():
    for data in [b"", b"\x00", b"\x00\x00abc", bytes(range(32)), sha256(b"x")]:
        assert base58_decode(base58_encode(data)) == data
    cid = to_cidv0(sha256(b"hello"))
    raw = base58_decode(cid)
    assert raw[0] == 0x12 and raw[1] == 0x20  # sha2-256 multihash, len 32
    assert raw[2:] == sha256(b"hello")
    with pytest.raises(ValueError):
        to_cidv0(b"short")


def test_swarm_bmt_deterministic_and_input_sensitive():
    a = swarm_bmt_single(b'{"x":1}', "bzzr1")
    b = swarm_bmt_single(b'{"x":2}', "bzzr1")
    assert len(a) == 32 and a != b
    # bzzr0 与 bzzr1 单分块计算相同
    assert swarm_bmt_single(b"abc", "bzzr0") == swarm_bmt_single(b"abc", "bzzr1")
    # 空输入也可计算（span=0 的 BMT 是确定值）
    assert len(swarm_bmt_single(b"", "bzzr0")) == 32
