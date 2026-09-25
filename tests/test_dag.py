"""DAG construction and shape-inference tests."""

import pytest

from tmem.dag import ADD, MATMUL, SLICE, build_dag
from tmem.dag import DAGValidationError


_UNSET = object()


def _req(inputs=_UNSET, ops=_UNSET, outputs=_UNSET):
    return {
        "inputs": {"a": [2, 3], "b": [2, 3]} if inputs is _UNSET else inputs,
        "ops": (
            [{"name": "c", "op": "add", "inputs": ["a", "b"]}]
            if ops is _UNSET
            else ops
        ),
        "outputs": ["c"] if outputs is _UNSET else outputs,
    }


def test_simple_add_shape():
    dag = build_dag(_req())
    assert dag.nodes["c"].shape == (2, 3)
    assert dag.consumers["a"] == [("c", 0)]
    assert dag.consumers["b"] == [("c", 1)]


def test_matmul_shape_rules():
    cases = [
        (([4, 5], [5, 7]), (4, 7)),       # 2D x 2D
        (([5], [5, 7]), (7,)),            # vector x matrix
        (([4, 5], [5]), (4,)),            # matrix x vector
        (([5], [5]), ()),                 # dot product -> scalar
        (([2, 3, 4, 5], [2, 3, 5, 6]), (2, 3, 4, 6)),  # batched
        (([3, 4, 5], [5, 2]), (3, 4, 2)),  # broadcast batch
    ]
    for (sa, sb), expected in cases:
        req = {
            "inputs": {"a": list(sa), "b": list(sb)},
            "ops": [{"name": "y", "op": MATMUL, "inputs": ["a", "b"]}],
            "outputs": ["y"],
        }
        dag = build_dag(req)
        assert dag.nodes["y"].shape == expected, (sa, sb, expected)


def test_add_broadcasting():
    req = {
        "inputs": {"a": [2, 3, 4], "b": [4]},
        "ops": [{"name": "y", "op": ADD, "inputs": ["a", "b"]}],
        "outputs": ["y"],
    }
    assert build_dag(req).nodes["y"].shape == (2, 3, 4)
    req["inputs"] = {"a": [1, 4], "b": [3, 1]}
    assert build_dag(req).nodes["y"].shape == (3, 4)


def test_slice_shape_and_metadata():
    req = {
        "inputs": {"x": [4, 6]},
        "ops": [
            {
                "name": "v",
                "op": SLICE,
                "inputs": ["x"],
                "starts": [1, 2],
                "stops": [4, 5],
            }
        ],
        "outputs": ["v"],
    }
    dag = build_dag(req)
    assert dag.nodes["v"].shape == (3, 3)
    assert dag.nodes["v"].slice_spec.starts == (1, 2)


def test_chained_slice_validates_against_current_view():
    # v = x[1:4, ...] has shape (3, 6); vv ranges must fit in 3, not 4.
    req = {
        "inputs": {"x": [4, 6]},
        "ops": [
            {"name": "v", "op": SLICE, "inputs": ["x"],
             "starts": [1, 0], "stops": [4, 6]},
            {"name": "vv", "op": SLICE, "inputs": ["v"],
             "starts": [0, 0], "stops": [4, 6]},  # axis 0 exceeds 3
        ],
        "outputs": ["vv"],
    }
    with pytest.raises(DAGValidationError, match="out of bounds"):
        build_dag(req)


@pytest.mark.parametrize(
    "payload,match",
    [
        (_req(inputs={}), "non-empty object"),
        (_req(inputs={"a": [0, 3]}), "positive integer"),
        (_req(inputs={"a": [2, 3.0]}), "positive integer"),
        (_req(ops=[{"name": "a", "op": ADD, "inputs": ["a", "b"]}]),
         "already defined"),
        (_req(ops=[{"name": "c", "op": "div", "inputs": ["a", "b"]}]),
         "unsupported op"),
        (_req(ops=[{"name": "c", "op": ADD, "inputs": ["a", "z"]}]),
         "unknown input"),
        (_req(ops=[{"name": "c", "op": ADD, "inputs": ["a", "a", "b"]}]),
         "exactly 2 inputs"),
        (_req(ops=[{"name": "c", "op": SLICE, "inputs": ["a"],
                   "starts": [0], "stops": [2]}]),
         "axis ranges"),
        (_req(outputs=["ghost"]), "unknown output"),
    ],
)
def test_invalid_requests(payload, match):
    with pytest.raises(DAGValidationError, match=match):
        build_dag(payload)


def test_matmul_contraction_mismatch():
    req = {
        "inputs": {"a": [2, 3], "b": [4, 2]},
        "ops": [{"name": "y", "op": MATMUL, "inputs": ["a", "b"]}],
        "outputs": ["y"],
    }
    with pytest.raises(DAGValidationError, match="contraction mismatch"):
        build_dag(req)


def test_duplicate_output_rejected():
    with pytest.raises(DAGValidationError, match="duplicate output"):
        build_dag(_req(outputs=["c", "c"]))


def test_slice_out_of_bounds():
    req = {
        "inputs": {"x": [4, 6]},
        "ops": [{"name": "v", "op": SLICE, "inputs": ["x"],
                 "starts": [0, 0], "stops": [5, 6]}],
        "outputs": ["v"],
    }
    with pytest.raises(DAGValidationError, match="out of bounds"):
        build_dag(req)


def test_vector_matmul_mismatches():
    with pytest.raises(DAGValidationError, match="vector matmul"):
        build_dag(
            {
                "inputs": {"a": [3], "b": [4]},
                "ops": [{"name": "y", "op": MATMUL, "inputs": ["a", "b"]}],
                "outputs": ["y"],
            }
        )
    with pytest.raises(DAGValidationError, match="contraction mismatch"):
        build_dag(
            {
                "inputs": {"a": [3], "b": [4, 2]},
                "ops": [{"name": "y", "op": MATMUL, "inputs": ["a", "b"]}],
                "outputs": ["y"],
            }
        )
    with pytest.raises(DAGValidationError, match="contraction mismatch"):
        build_dag(
            {
                "inputs": {"a": [3, 4], "b": [5]},
                "ops": [{"name": "y", "op": MATMUL, "inputs": ["a", "b"]}],
                "outputs": ["y"],
            }
        )


def test_batched_matmul_broadcast_mismatch():
    with pytest.raises(DAGValidationError, match="not broadcast-compatible"):
        build_dag(
            {
                "inputs": {"a": [2, 3, 4, 5], "b": [3, 3, 5, 2]},
                "ops": [{"name": "y", "op": MATMUL, "inputs": ["a", "b"]}],
                "outputs": ["y"],
            }
        )


def test_slice_spec_errors():
    base = {
        "inputs": {"x": [4, 6]},
        "outputs": ["v"],
    }
    cases = [
        ({"name": "v", "op": SLICE, "inputs": ["x"]},
         "requires integer-list"),
        ({"name": "v", "op": SLICE, "inputs": ["x"],
          "starts": [0, 0], "stops": [4]},
         "length differ"),
        ({"name": "v", "op": SLICE, "inputs": ["x"],
          "starts": [0, "0"], "stops": [4, 6]},
         r"starts\[1\]"),
        ({"name": "v", "op": SLICE, "inputs": ["x"],
          "starts": [0, 0], "stops": [4, 6.0]},
         r"stops\[1\]"),
    ]
    for op, match in cases:
        req = {**base, "ops": [op]}
        with pytest.raises(DAGValidationError, match=match):
            build_dag(req)


def test_request_envelope_errors():
    with pytest.raises(DAGValidationError, match="must be an object"):
        build_dag([1, 2, 3])
    with pytest.raises(DAGValidationError, match="'ops' must be a list"):
        build_dag({"inputs": {"a": [2]}, "ops": {}, "outputs": ["a"]})
    with pytest.raises(DAGValidationError, match="must be an object"):
        build_dag({"inputs": {"a": [2]}, "ops": ["x"], "outputs": ["a"]})
    with pytest.raises(DAGValidationError, match="requires a string 'name'"):
        build_dag(
            {"inputs": {"a": [2]},
             "ops": [{"op": ADD, "inputs": ["a", "a"]}],
             "outputs": ["a"]}
        )
    with pytest.raises(DAGValidationError, match="'inputs' must be a non-empty list"):
        build_dag(
            {"inputs": {"a": [2]},
             "ops": [{"name": "c", "op": ADD, "inputs": []}],
             "outputs": ["c"]}
        )


def test_scalar_dot_product_shape():
    dag = build_dag(
        {
            "inputs": {"a": [3], "b": [3]},
            "ops": [{"name": "d", "op": MATMUL, "inputs": ["a", "b"]}],
            "outputs": ["d"],
        }
    )
    assert dag.nodes["d"].shape == ()


def test_shapes_known_helper():
    dag = build_dag(_req())
    assert dag.shapes_known()

