"""库占位与链接核验：新版占位哈希绑定、EIP-55 地址、逐 nibble 掩码。"""

import pytest

from app.bytecode import (
    BytecodeError,
    diff_nibbles,
    find_placeholders,
    mask_regions,
    normalize_hex,
    normalize_unlinked_hex,
)
from app.libraries import (
    LibraryError,
    apply_linking,
    legacy_token,
    new_style_token,
)

FQN = "src/SafeMath.sol:SafeMath"


def test_new_style_token_is_real_keccak_binding():
    from app.crypto import keccak256

    token = new_style_token(FQN)
    middle = token[3:-3]
    assert len(token) == 40
    assert middle == keccak256(FQN.encode()).hex()[:34]


def test_legacy_token_padding_rule():
    t = legacy_token(FQN)
    assert len(t) == 40 and t.startswith("__") and t.endswith("__")


def test_normalize_unlinked_accepts_placeholder_but_rejects_garbage():
    token = new_style_token(FQN)
    s = "6080" + token + "00" * 10
    out = normalize_unlinked_hex("0x" + s)
    assert out == s
    with pytest.raises(BytecodeError):
        normalize_unlinked_hex("0x60zz80")
    with pytest.raises(BytecodeError):
        normalize_unlinked_hex("0x60__$broken$__")
    with pytest.raises(BytecodeError):
        normalize_unlinked_hex("0xabc")  # 奇数长度


def test_find_placeholders_locates_both_styles():
    token = new_style_token(FQN)
    phs = find_placeholders("ab" * 4 + token + "cd" * 4)
    assert phs == [(8, 48, token)]


def test_linking_writes_address_and_validates_binding():
    token = new_style_token(FQN)
    code = "ab" * 4 + token + "cd" * 4
    addr = "0x5aAeb6053F3E94C9b9A09f33669435E7Ef1BeAed"
    libs = {"src/SafeMath.sol": {"SafeMath": addr}}
    refs = [("src/SafeMath.sol", "SafeMath", 8, 40)]
    linked, ev = apply_linking(code, refs, libs)
    assert linked[8:48] == addr[2:].lower()
    assert ev[0]["style"] == "new"
    # 占位被真实替换、无残留
    assert find_placeholders(linked) == []


def test_linking_rejects_wrong_fqn_binding():
    token = new_style_token("other/Other.sol:Other")
    code = "ab" * 4 + token + "cd" * 4
    refs = [("src/SafeMath.sol", "SafeMath", 8, 40)]
    libs = {"src/SafeMath.sol": {"SafeMath": "0x5aAeb6053F3E94C9b9A09f33669435E7Ef1BeAed"}}
    with pytest.raises(LibraryError):
        apply_linking(code, refs, libs)


def test_linking_rejects_bad_checksum_address():
    token = new_style_token(FQN)
    refs = [("src/SafeMath.sol", "SafeMath", 8, 40)]
    libs = {"src/SafeMath.sol": {"SafeMath": "0x5AAeB6053F3E94C9B9A09F33669435E7EF1BEAed"}}
    with pytest.raises(LibraryError):
        apply_linking("ab" * 4 + token + "cd" * 4, refs, libs)


def test_mask_only_hides_declared_regions():
    a = "0011223344"
    b = "00992233AA"
    # 掩码 index 2,3 -> 差异 1 与 8,9 中，8,9 仍暴露
    mask = mask_regions(10, [(2, 2)])
    diffs = diff_nibbles(a, b, mask)
    assert diffs == [{"start_nibble": 8, "end_nibble": 10, "expected": "44", "actual": "AA"}]
    assert diff_nibbles(a, a, mask) == []


def test_overlapping_mask_regions_rejected():
    with pytest.raises(BytecodeError):
        mask_regions(10, [(0, 4), (2, 3)])


def test_length_difference_is_never_masked():
    assert normalize_hex("0xAabb") == "aabb"
    with pytest.raises(BytecodeError):
        normalize_hex("0xabc")
