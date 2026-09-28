"""Tests for the eager computation graph tracing and plan execution."""
from __future__ import annotations

import numpy as np

from tensor_backend.graph import ComputeGraph
from tensor_backend.tensor import Tensor


def test_transpose_reshape_plan_reports_aliasing():
    g = ComputeGraph(graph_id="g1")
    g.execute_plan({
        "steps": [
            {"op": "constant", "out": "x", "values": np.arange(12).reshape(3, 4).tolist()},
            {"op": "transpose", "out": "xt", "src": "x"},
            {"op": "reshape", "out": "xf", "src": "xt", "shape": [12], "allow_copy": True},
        ]
    })
    assert g.tensor("xt").shares_storage(g.tensor("x"))
    assert not g.tensor("xf").shares_storage(g.tensor("x"))
    trace = g.trace()
    assert trace[0]["opcode"] == "transpose" and trace[0]["aliases_storage"] is True
    assert trace[1]["opcode"] == "reshape" and trace[1]["copied"] is True


def test_zero_copy_reshape_plan_is_alias():
    g = ComputeGraph()
    g.constant("x", np.arange(12).reshape(3, 4))
    g.reshape("y", "x", [4, 3])
    node = g.trace()[-1]
    assert node["aliases_storage"] is True and node["copied"] is False


def test_slice_then_binary_trace():
    g = ComputeGraph()
    g.constant("a", [[1.0, 2.0, 3.0, 4.0]])
    g.constant("b", [[10.0, 20.0]])
    g.slice_view("a2", "a", [0, 0], [1, 2])
    g.slice_view("b2", "b", [0, 0], [1, 2])
    g.binary("c", "add", "a2", "b2")
    np.testing.assert_array_equal(g.tensor("c").materialize(), [[11.0, 22.0]])


def test_duplicate_handle_rejected():
    g = ComputeGraph()
    g.constant("x", [1.0])
    try:
        g.constant("x", [2.0])
    except KeyError:
        return
    raise AssertionError("duplicate handle must raise KeyError")


def test_aliasing_report_flags_self_overlap():
    g = ComputeGraph()
    g.add_tensor("ov", Tensor.from_layout(
        np.arange(3.0), shape=(3, 3), strides=(0, 1)))
    report = {r["handle"]: r for r in g.aliasing_report()}
    assert report["ov"]["self_overlapping"] is True
