import pytest

from mde.paths import (
    ARRAY,
    PathError,
    decode_segment,
    encode_segment,
    instance_tokens,
    join_tokens,
    prefix_of,
    shape_of_instance,
    split_path,
)


def test_roundtrip_simple_and_encoded_segments():
    for raw in ["user.id", "a[]b", "100%", "normal_key", "x[y]", "", "点"]:
        seg = encode_segment(raw)
        assert decode_segment(seg) == raw


def test_literal_dotted_key_is_distinct_from_nested_path():
    literal = join_tokens([encode_segment("user.id")])
    nested = join_tokens(["user", "id"])
    assert literal == "user%2eid"
    assert nested == "user.id"
    assert split_path(literal) == ["user%2eid"]
    assert split_path(nested) == ["user", "id"]


def test_split_array_paths():
    assert split_path("orders[].amount") == ["orders", ARRAY, "amount"]
    assert join_tokens(split_path("orders[].amount")) == "orders[].amount"
    assert join_tokens(split_path("tags[]")) == "tags[]"


def test_split_rejects_bad_paths():
    bad = ["", ".", "a..b", ".a", "a.", "a[0]", "a]", "a.[", "a[].x[]b"]
    for p in bad:
        with pytest.raises(PathError):
            split_path(p)
    # 策略规则路径默认不允许具体下标；显式开启后用于解析实例路径
    with pytest.raises(PathError):
        split_path("orders[0].id")
    assert split_path("orders[0].id", allow_indices=True) == ["orders", "[0]", "id"]
    # 斜杠不是路径保留字符，带斜杠的键合法（encode_segment 不编码它）
    assert split_path("a/b") == ["a/b"]
    assert decode_segment(encode_segment("a/b")) == "a/b"


def test_instance_rendering():
    shape = split_path("orders[].items[].sku")
    rendered = join_tokens(instance_tokens(shape, {0: 2, 1: 0}))
    assert rendered == "orders[2].items[0].sku"


def test_shape_matching():
    shape = split_path("orders[].amount")
    inst = ["orders", "[3]", "amount"]
    assert shape_of_instance(shape, inst)
    assert not shape_of_instance(shape, ["orders", "amount"])
    assert prefix_of(split_path("orders"), shape)
    assert not prefix_of(split_path("orders[].id"), shape)
