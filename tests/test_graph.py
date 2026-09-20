"""Graph validation: cycles, shapes, parameter ranges."""
from __future__ import annotations

import pytest

from app.graph import (
    GraphValidationError,
    build_model,
    normalize_spec,
    output_dim,
)
import torch


def test_valid_linear_dag_forward_shapes():
    spec = normalize_spec({
        "input_features": 4,
        "layers": [
            {"name": "h", "type": "dense", "out_features": 8, "input": "input"},
            {"name": "a", "type": "relu", "input": "h"},
            {"name": "o", "type": "dense", "out_features": 3, "input": "a"},
        ],
    })
    model = build_model(spec)
    out = model(torch.zeros(5, 4))
    assert out.shape == (5, 3)
    assert output_dim(spec) == 3


def test_dense_wiring_to_dropout_and_relu_preserves_width():
    spec = normalize_spec({
        "input_features": 6,
        "layers": [
            {"name": "h", "type": "dense", "out_features": 10, "input": "input"},
            {"name": "drop", "type": "dropout", "p": 0.5, "input": "h"},
            {"name": "act", "type": "relu", "input": "drop"},
            {"name": "o", "type": "dense", "out_features": 2, "input": "act"},
        ],
    })
    assert output_dim(spec) == 2


def test_cycle_rejected():
    with pytest.raises(GraphValidationError, match="cycle"):
        normalize_spec({
            "input_features": 3,
            "layers": [
                {"name": "a", "type": "dense", "out_features": 4, "input": "b"},
                {"name": "b", "type": "relu", "input": "a"},
                {"name": "o", "type": "dense", "out_features": 2, "input": "b"},
            ],
        })


def test_self_loop_rejected():
    with pytest.raises(GraphValidationError, match="itself"):
        normalize_spec({
            "input_features": 3,
            "layers": [
                {"name": "a", "type": "relu", "input": "a"},
            ],
        })


def test_unknown_input_layer():
    with pytest.raises(GraphValidationError, match="unknown input layer"):
        normalize_spec({
            "input_features": 3,
            "layers": [
                {"name": "a", "type": "relu", "input": "ghost"},
            ],
        })


def test_multiple_outputs_rejected():
    with pytest.raises(GraphValidationError, match="exactly one output"):
        normalize_spec({
            "input_features": 3,
            "layers": [
                {"name": "a", "type": "dense", "out_features": 4, "input": "input"},
                {"name": "b", "type": "relu", "input": "input"},
            ],
        })


def test_duplicate_layer_names_rejected():
    with pytest.raises(GraphValidationError, match="duplicate"):
        normalize_spec({
            "input_features": 3,
            "layers": [
                {"name": "x", "type": "dense", "out_features": 4, "input": "input"},
                {"name": "x", "type": "relu", "input": "x"},
            ],
        })


def test_bad_layer_type():
    with pytest.raises(GraphValidationError, match="type must be one"):
        normalize_spec({
            "input_features": 3,
            "layers": [
                {"name": "x", "type": "conv2d", "input": "input"},
            ],
        })


@pytest.mark.parametrize("features", [0, -1, 100_001])
def test_input_features_range(features):
    with pytest.raises(GraphValidationError):
        normalize_spec({"input_features": features, "layers": [
            {"name": "o", "type": "dense", "out_features": 1, "input": "input"},
        ]})


@pytest.mark.parametrize("out", [0, -4])
def test_dense_out_features_range(out):
    with pytest.raises(GraphValidationError, match="out_features"):
        normalize_spec({"input_features": 3, "layers": [
            {"name": "o", "type": "dense", "out_features": out, "input": "input"},
        ]})


@pytest.mark.parametrize("p", [-0.1, 1.0, 2.0])
def test_dropout_p_range(p):
    with pytest.raises(GraphValidationError, match="p must be"):
        normalize_spec({"input_features": 3, "layers": [
            {"name": "d", "type": "dropout", "p": p, "input": "input"},
        ]})


def test_empty_layers_rejected():
    with pytest.raises(GraphValidationError):
        normalize_spec({"input_features": 3, "layers": []})
