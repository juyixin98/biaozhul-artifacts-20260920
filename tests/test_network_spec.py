"""Shape errors and other network-spec validation failures."""
from __future__ import annotations

import pytest

from app.models.network_spec import SpecError, parse_spec


def base_layers():
    return [
        {"id": "in", "type": "input", "in_features": 4},
        {"id": "h", "type": "dense", "out_features": 8},
        {"id": "a", "type": "relu"},
        {"id": "d", "type": "dropout", "p": 0.2},
        {"id": "out", "type": "dense", "out_features": 3},
    ]


def base_edges():
    return [["in", "h"], ["h", "a"], ["a", "d"], ["d", "out"]]


def test_valid_spec_builds():
    spec = parse_spec({"layers": base_layers(), "connections": base_edges()})
    assert spec.in_features == 4
    assert spec.out_features == 3


def test_cycle_rejected():
    with pytest.raises(SpecError, match="cycle"):
        parse_spec({
            "layers": [
                {"id": "in", "type": "input", "in_features": 2},
                {"id": "b", "type": "relu"},
                {"id": "c", "type": "relu"},
            ],
            "connections": [["in", "b"], ["b", "c"], ["c", "b"]],
        })


def test_unsupported_layer_type():
    with pytest.raises(SpecError, match="unsupported type"):
        parse_spec({
            "layers": [
                {"id": "in", "type": "input", "in_features": 2},
                {"id": "b", "type": "conv2d"},
            ],
            "connections": [["in", "b"]],
        })


def test_dropout_param_range():
    layers = base_layers()
    layers[3]["p"] = 1.5
    with pytest.raises(SpecError, match="out of range"):
        parse_spec({"layers": layers, "connections": base_edges()})


def test_dense_feature_range():
    layers = [
        {"id": "in", "type": "input", "in_features": 4},
        {"id": "big", "type": "dense", "out_features": 10_000},
    ]
    with pytest.raises(SpecError, match="out of range"):
        parse_spec({"layers": layers,
                    "connections": [["in", "big"]]})


def test_missing_layer_param():
    layers = [
        {"id": "in", "type": "input"},
        {"id": "o", "type": "dense", "out_features": 2},
    ]
    with pytest.raises(SpecError, match="in_features"):
        parse_spec({"layers": layers, "connections": [["in", "o"]]})


def test_dangling_connection():
    with pytest.raises(SpecError, match="unknown layer"):
        parse_spec({"layers": base_layers(),
                    "connections": base_edges() + [["a", "ghost"]]})


def test_two_inputs_rejected():
    layers = base_layers() + [{"id": "in2", "type": "input", "in_features": 4}]
    with pytest.raises(SpecError, match="exactly one input"):
        parse_spec({"layers": layers, "connections": base_edges()})


def test_duplicate_layer_id():
    layers = base_layers()
    layers[1]["id"] = "in"
    with pytest.raises(SpecError, match="duplicate"):
        parse_spec({"layers": layers, "connections": base_edges()})


def test_forward_shape_mismatch_at_terminal():
    # Two terminal layers of different widths is rejected as multi-terminal;
    # a genuine width mismatch surfaces when a later dense sees a wrong width
    # is impossible here (dense reshapes), so assert a split/merge mismatch:
    layers = [
        {"id": "in", "type": "input", "in_features": 2},
        {"id": "b", "type": "dense", "out_features": 4},
        {"id": "c", "type": "dense", "out_features": 5},
        {"id": "r", "type": "relu"},
        {"id": "o", "type": "dense", "out_features": 1},
    ]
    with pytest.raises(SpecError, match="shape mismatch|terminal"):
        parse_spec({
            "layers": layers,
            "connections": [["in", "b"], ["in", "c"], ["b", "r"], ["c", "r"],
                            ["r", "o"]],
        })
