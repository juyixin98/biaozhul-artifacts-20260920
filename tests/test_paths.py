import pytest

from maskcompiler.errors import PathSyntaxError
from maskcompiler.paths import match, parse_path, render_location


def locs(tokens, doc):
    return [loc for loc, _ in match(tokens, doc)]


def test_simple_key_and_root():
    doc = {"a": {"b": 1}}
    assert locs(parse_path("$.a.b"), doc) == [("a", "b")]
    assert locs(parse_path("$"), doc) == [()]


def test_array_wildcard_nested():
    doc = {"users": [{"phone": "1"}, {"phone": "2"}, {"no": "x"}]}
    assert locs(parse_path("$.users[*].phone"), doc) == [
        ("users", 0, "phone"),
        ("users", 1, "phone"),
    ]


def test_array_index_including_negative():
    doc = {"items": ["a", "b", "c"]}
    assert locs(parse_path("$.items[0]"), doc) == [("items", 0)]
    assert locs(parse_path("$.items[-1]"), doc) == [("items", 2)]
    assert locs(parse_path("$.items[9]"), doc) == []


def test_recursive_key():
    doc = {"name": "top", "kids": [{"name": "a"}, {"child": {"name": "b"}}]}
    found = locs(parse_path("$..name"), doc)
    assert found == [("name",), ("kids", 0, "name"), ("kids", 1, "child", "name")]


def test_recursive_array_wildcard():
    doc = {"groups": [{"tags": ["x", "y"]}, {"tags": []}], "tags": ["z"]}
    found = locs(parse_path("$..[*]"), doc)
    # top-level "tags" array elements and all "tags" array elements at depth
    assert ("tags", 0) in found
    assert ("groups", 0, "tags", 0) in found
    assert ("groups", 0, "tags", 1) in found


def test_quoted_keys_with_special_chars():
    doc = {"x-y": {"a.b": 2}, "中文键": 3}
    assert locs(parse_path("$['x-y']['a.b']"), doc) == [("x-y", "a.b")]
    assert locs(parse_path('$["中文键"]'), doc) == [("中文键",)]
    assert locs(parse_path("$.中文键"), doc) == [("中文键",)]


def test_structural_mismatch_produces_no_matches():
    assert locs(parse_path("$.a.b"), {"a": [1, 2]}) == []
    # an index segment on a non-array simply selects nothing
    assert locs(parse_path("$.items[0]"), {"items": {"k": "v"}}) == []


def test_any_segment_on_object_yields_values():
    assert locs(parse_path("$.items[*]"), {"items": {"k": "v", "j": "w"}}) == [
        ("items", "k"),
        ("items", "j"),
    ]


def test_values_are_returned():
    doc = {"users": [{"phone": "13800000000"}]}
    pairs = match(parse_path("$.users[*].phone"), doc)
    assert pairs[0][1] == "13800000000"


@pytest.mark.parametrize(
    "bad",
    ["", " ", "a.b", "$..", "$. ", "$[", "$.[]", "$['unclosed", "$[abc]", "$..*[0]x"],
)
def test_invalid_paths(bad):
    with pytest.raises(PathSyntaxError):
        parse_path(bad)


def test_render_location_is_data_free():
    assert render_location(("users", 0, "phone")) == "$.users[0].phone"
    # hyphen is a legal bare-name character in this grammar, so it stays bare
    assert render_location(("a-b",)) == "$.a-b"
    assert render_location(("a.b",)) == "$['a.b']"
