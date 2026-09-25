from dms.paths import _RECURSIVE, canonical, find_matches, parse_path
import pytest


def test_simple_dotted_path():
    assert parse_path("$.foo.bar") == ("foo", "bar")
    assert parse_path("foo.bar") == ("foo", "bar")


def test_wildcard_segments():
    segs = parse_path("$.users[*].addresses[*].city")
    assert segs == ("users", "*", "addresses", "*", "city")


def test_bracket_quoted_keys_with_special_chars():
    assert parse_path('$["a.b"]["x y"]') == ("a.b", "x y")
    assert parse_path("$['单引号键']") == ("单引号键",)


def test_array_index():
    assert parse_path("$.items[2].id") == ("items", 2, "id")


def test_recursive_descent():
    segs = parse_path("$..note")
    assert segs[0] is _RECURSIVE
    assert segs[1] == "note"


def test_recursive_requires_following_segment():
    with pytest.raises(ValueError):
        parse_path("$..")


def test_reject_empty_and_garbage():
    with pytest.raises(ValueError):
        parse_path("")
    with pytest.raises(ValueError):
        parse_path("   ")
    with pytest.raises(ValueError):
        parse_path("foo..")
    with pytest.raises(ValueError):
        parse_path("foo.[")
    with pytest.raises(ValueError):
        parse_path("items[-1]")
    with pytest.raises(ValueError):
        parse_path("$..")


def test_root_wildcard_is_valid():
    assert parse_path("$[*]") == ("*",)


def test_matches_nested_arrays():
    doc = {
        "users": [
            {"addresses": [{"city": "北京"}, {"city": "上海"}]},
            {"addresses": [{"city": "深圳"}]},
        ]
    }
    trails = find_matches(doc, parse_path("$.users[*].addresses[*].city"))
    values = [_get(doc, t) for t in trails]
    assert values == ["北京", "上海", "深圳"]


def test_matches_array_index():
    doc = {"items": [{"id": 0}, {"id": 1}, {"id": 2}]}
    trails = find_matches(doc, parse_path("$.items[1].id"))
    assert trails == [("items", 1, "id")]


def test_matches_recursive_descent_all_depths():
    doc = {"note": "a", "x": {"note": "b", "y": [{"note": "c"}, {"other": 1}]}}
    trails = find_matches(doc, parse_path("$..note"))
    assert sorted(str(t) for t in trails) == sorted(["('note',)",
                                                     "('x', 'note')",
                                                     "('x', 'y', 0, 'note')"])


def test_missing_field_produces_no_matches():
    doc = {"a": {"b": 1}}
    assert find_matches(doc, parse_path("$.a.c")) == []
    assert find_matches(doc, parse_path("$.x[*].y")) == []


def test_wildcard_over_objects():
    doc = {"a": {"k": 1}, "b": {"k": 2}}
    trails = find_matches(doc, parse_path("$[*].k"))
    assert set(trails) == {("a", "k"), ("b", "k")}


def test_canonical_normalizes_equivalent_paths():
    assert canonical("$.users[*].id") == canonical("users[*].id")
    assert canonical("$['x'].y") == "$.x.y"


def test_index_out_of_range_no_match():
    assert find_matches([1, 2], parse_path("$[5]")) == []


def _get(doc, trail):
    for p in trail:
        doc = doc[p]
    return doc
