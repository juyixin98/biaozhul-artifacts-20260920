"""路径规范化：任何语义歧义必须拒绝，规范化不得掩盖差异。"""

import pytest

from app.paths import UnsafePath, canonical_source_path


@pytest.mark.parametrize(
    "bad",
    [
        "../escape.sol",
        "a/../../b.sol",
        "a/./b.sol",
        "a//b.sol",
        "a/b/",
        "/etc/passwd",
        "C:\\Windows\\x.sol",
        "..\\..\\escape.sol",
        "a\\b.sol",
        "a\x00b.sol",
        "a/b\rc.sol",
        "",
        ".",
        "..",
        "a/..",
        "a/../b",
        "  ",
    ],
)
def test_unsafe_paths_rejected(bad):
    with pytest.raises(UnsafePath):
        canonical_source_path(bad)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("src/Vault.sol", "src/Vault.sol"),
        ("a/b/c.sol", "a/b/c.sol"),
        ("lib/SafeMath.sol", "lib/SafeMath.sol"),
    ],
)
def test_clean_paths_preserved(raw, expected):
    assert canonical_source_path(raw) == expected


def test_normalization_does_not_mask_semantic_differences():
    # 大小写差异被保留（不做折叠）
    assert canonical_source_path("A.sol") != canonical_source_path("a.sol")
    # 前导/尾随空白只是 strip，但反斜杠绝不折叠为正斜杠
    assert canonical_source_path("  a/b.sol  ") == "a/b.sol"
