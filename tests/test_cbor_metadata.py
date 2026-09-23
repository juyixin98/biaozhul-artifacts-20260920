"""CBOR 元数据尾解析与哈希真实重算。"""

import cbor2
import pytest

from app import crypto
from app.metadata import (
    MetadataError,
    decode_cbor_segment,
    parse_cbor_tail,
    verify_embedded_hash,
)


def _wrap(cbor_seg: bytes) -> str:
    return (b"\x60" * 20 + cbor_seg + len(cbor_seg).to_bytes(2, "big")).hex()


def test_decode_solc_style_map():
    seg = cbor2.dumps({"ipfs": b"\x11" * 32, "solc": bytes([8, 24, 0])}, canonical=True)
    obj = decode_cbor_segment(seg)
    assert obj["ipfs"] == b"\x11" * 32
    assert obj["solc"] == bytes([8, 24, 0])


def test_trailing_bytes_rejected():
    seg = cbor2.dumps({"a": 1}) + b"\x00"
    with pytest.raises(MetadataError):
        decode_cbor_segment(seg)


def test_indefinite_length_rejected():
    # 不定长数组 0x9f ... 0xff 必须被拒绝（解析器只接受确定长度）
    with pytest.raises(MetadataError):
        decode_cbor_segment(bytes([0x9F, 0x01, 0x02, 0xFF]))


def test_parse_tail_length_and_offset():
    seg = cbor2.dumps({"ipfs": b"\xaa" * 32, "solc": bytes([0, 8, 24])}, canonical=True)
    hexcode = _wrap(seg)
    obj, start = parse_cbor_tail(hexcode)
    assert obj["ipfs"] == b"\xaa" * 32
    assert start == 40  # 20 字节前缀


def test_bad_tail_length_declared():
    body = bytearray.fromhex("60" * 30)
    body[-2:] = (0xFFFF).to_bytes(2, "big")  # 声称尾长 65535，实际很短
    with pytest.raises(MetadataError):
        parse_cbor_tail(body.hex())


def test_embedded_ipfs_hash_must_match_real_metadata():
    metadata_raw = b'{"compiler":{"version":"0.8.24"}}'
    good = crypto.sha256(metadata_raw)
    cbor = {"ipfs": good, "solc": bytes([0, 8, 24])}
    res = verify_embedded_hash(metadata_raw, cbor)
    ipfs = [h for h in res["hash_checks"] if h["scheme"] == "ipfs"][0]
    assert ipfs["ok"] is True
    assert ipfs["computed"] == good.hex()
    assert ipfs["cidv0"].startswith("Qm")
    # 篡改元数据一个字节 -> 必须失败
    bad_cbor = {"ipfs": good, "solc": bytes([0, 8, 24])}
    res2 = verify_embedded_hash(metadata_raw + b" ", bad_cbor)
    assert res2["hash_checks"][0]["ok"] is False


def test_bzzr_scheme_verified_against_bmt():
    metadata_raw = b'{"a":1}'
    digest = crypto.swarm_bmt_single(metadata_raw, "bzzr1")
    res = verify_embedded_hash(metadata_raw, {"bzzr1": digest})
    assert res["hash_checks"][0]["ok"] is True
    res_bad = verify_embedded_hash(metadata_raw + b"x", {"bzzr1": digest})
    assert res_bad["hash_checks"][0]["ok"] is False


def test_unknown_keys_are_reported_not_dropped():
    seg = cbor2.dumps({"ipfs": b"\x01" * 32, "futureField": 7}, canonical=True)
    obj = decode_cbor_segment(seg)
    res = verify_embedded_hash(b"m", obj)
    assert "futureField" in res["unknown_keys"]
