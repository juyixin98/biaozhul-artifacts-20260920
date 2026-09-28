"""Tests for parameter-graph invariants and training input boundaries."""

import numpy as np
import pytest

from bucket_sync.config import make_graph
from bucket_sync.graph import GraphError, ParameterGraph, ParameterNode
from bucket_sync.training import (
    TrainingError,
    build_linear_graph,
    linear_mse_gradients,
)
from bucket_sync.tensor_types import TensorSpec

pytestmark = pytest.mark.unit


def test_graph_order_is_preserved_and_lookup_works():
    graph = make_graph(3)
    assert graph.param_names() == ("w", "b")
    assert graph.trainable_names() == ("w", "b")
    assert graph.placeholder_names() == ()
    assert graph.total_size() == 4
    assert graph.node("w").spec.shape == (3, 1)
    assert "linear-mse" in graph.fingerprint()


def test_graph_rejects_empty_name_duplicates_and_empty_nodes():
    with pytest.raises(GraphError):
        ParameterGraph("", [ParameterNode(TensorSpec("w", (1,)))])
    with pytest.raises(GraphError):
        ParameterGraph("g", [])
    with pytest.raises(GraphError):
        ParameterGraph(
            "g",
            [ParameterNode(TensorSpec("w", (1,))), ParameterNode(TensorSpec("w", (2,)))],
        )


def test_unknown_parameter_lookup_raises(graph):
    with pytest.raises(GraphError, match="unknown parameter"):
        graph.node("nope")
    with pytest.raises(GraphError):
        graph.spec("nope")


def test_frozen_bias_is_listed_as_placeholder():
    graph = build_linear_graph(2)
    nodes = list(graph.nodes)
    frozen = ParameterGraph(
        "frozen",
        [nodes[0], ParameterNode(nodes[1].spec, trainable=False)],
    )
    assert frozen.placeholder_names() == ("b",)
    assert frozen.trainable_names() == ("w",)


def test_linear_gradients_reject_bad_shapes_and_non_finite():
    w = np.zeros((3, 1))
    b = np.zeros(1)
    with pytest.raises(TrainingError, match="2-D"):
        linear_mse_gradients({"w": w, "b": b}, np.zeros((4, 3, 1)), np.zeros((4, 1)))
    with pytest.raises(TrainingError, match="empty batch"):
        linear_mse_gradients({"w": w, "b": b}, np.zeros((0, 3)), np.zeros((0, 1)))
    with pytest.raises(TrainingError, match="y must have shape"):
        linear_mse_gradients({"w": w, "b": b}, np.ones((4, 3)), np.zeros((4,)))
    with pytest.raises(TrainingError, match="features"):
        linear_mse_gradients({"w": np.zeros((2, 1)), "b": b}, np.ones((4, 3)), np.zeros((4, 1)))
    with pytest.raises(TrainingError, match="NaN or Inf"):
        linear_mse_gradients(
            {"w": w, "b": b},
            np.full((4, 3), np.inf),
            np.zeros((4, 1)),
        )


def test_linear_gradients_are_sums_not_means():
    # Gradient sums must scale with the number of identical rows.
    w = np.array([[1.0], [0.0]])
    b = np.array([0.0])
    x1 = np.array([[1.0, 0.0]])
    y1 = np.array([[0.0]])
    g1, n1 = linear_mse_gradients({"w": w, "b": b}, x1, y1)
    x2 = np.vstack([x1, x1])
    y2 = np.array([[0.0], [0.0]])
    g2, n2 = linear_mse_gradients({"w": w, "b": b}, x2, y2)
    assert n1 == 1 and n2 == 2
    np.testing.assert_allclose(g2["w"], 2 * g1["w"])
    np.testing.assert_allclose(g2["b"], 2 * g1["b"])


def test_model_state_requires_every_parameter_and_copies_on_update(graph, model):
    with pytest.raises(TrainingError):
        type(model)(graph, {"w": model.params["w"]})
    updated = model.copy_with({"w": model.params["w"] + 1.0})
    # immutability: original unchanged, new state has bumped step
    assert updated.step == model.step + 1
    assert model.step == 0
    np.testing.assert_allclose(model.params["w"], updated.params["w"] - 1.0)
