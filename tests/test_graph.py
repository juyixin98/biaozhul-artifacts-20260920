"""Tests for graph construction, validation and connectivity diagnostics."""

import numpy as np
import pytest

from pose_graph.graph import GraphStructureError, PoseGraph


def _info():
    return np.diag([1.0, 1.0, 1.0])


def test_add_nodes_and_edges_and_fix():
    g = PoseGraph()
    g.add_node(2, [0, 0, 0])
    g.add_node(0, [1, 1, 0], fixed=True)
    g.add_node(1, [2, 0, 0])
    g.add_edge(0, 1, [1, -1, 0], _info())
    g.add_edge(1, 2, [1, 0, 0], _info())
    assert g.num_nodes() == 3
    assert g.ordered_indices() == [0, 1, 2]
    assert g.fixed_nodes == {0}


def test_duplicate_node_rejected():
    g = PoseGraph()
    g.add_node(0, [0, 0, 0])
    with pytest.raises(GraphStructureError):
        g.add_node(0, [1, 1, 1])


def test_edge_to_unknown_node_rejected():
    g = PoseGraph()
    g.add_node(0, [0, 0, 0])
    with pytest.raises(GraphStructureError):
        g.add_edge(0, 9, [1, 0, 0], _info())


def test_bad_pose_and_measurement_shapes():
    g = PoseGraph()
    with pytest.raises(GraphStructureError):
        g.add_node(0, [0, 0])
    g.add_node(0, [0, 0, 0])
    g.add_node(1, [1, 0, 0])
    with pytest.raises(GraphStructureError):
        g.add_edge(0, 1, [1, 0], _info())
    with pytest.raises(GraphStructureError):
        g.add_edge(0, 1, [1, 0, 0], np.eye(2))


def test_information_must_be_psd():
    g = PoseGraph()
    g.add_node(0, [0, 0, 0])
    g.add_node(1, [1, 0, 0])
    with pytest.raises(GraphStructureError):
        g.add_edge(0, 1, [1, 0, 0], np.diag([1.0, -1.0, 1.0]))


def test_symmetric_part_of_information_is_used():
    g = PoseGraph()
    g.add_node(0, [0, 0, 0])
    g.add_node(1, [1, 0, 0])
    info = np.array([[2, 1, 0], [-1, 2, 0], [0, 0, 1]], float)
    e = g.add_edge(0, 1, [1, 0, 0], info)
    np.testing.assert_allclose(e.information, e.information.T)


def test_single_connected_component():
    g = PoseGraph()
    for k in range(4):
        g.add_node(k, [k, 0, 0], fixed=(k == 0))
    for k in range(3):
        g.add_edge(k, k + 1, [1, 0, 0], _info())
    report = g.diagnose()
    assert report["is_connected"] is True
    assert report["num_components"] == 1
    assert report["unanchored_components"] == []


def test_two_components_diagnostics_and_auto_anchor_membership():
    g = PoseGraph()
    for k in range(4):
        g.add_node(k, [k, 0, 0], fixed=(k == 0))
    g.add_edge(0, 1, [1, 0, 0], _info())
    # Isolated pair 2-3 with no fixed node.
    g.add_edge(2, 3, [1, 0, 0], _info())
    components = g.connected_components()
    assert len(components) == 2
    report = g.diagnose()
    assert report["is_connected"] is False
    unanchored = report["unanchored_components"]
    assert unanchored == [[2, 3]]
    assert report["components"][0]["anchored"] is True
    assert report["components"][1]["anchored"] is False


def test_isolated_single_node_is_its_own_component():
    g = PoseGraph()
    g.add_node(0, [0, 0, 0], fixed=True)
    g.add_node(1, [1, 0, 0])
    g.add_node(2, [5, 5, 0])
    g.add_edge(0, 1, [1, 0, 0], _info())
    report = g.diagnose()
    assert report["num_components"] == 2
    assert [2] in report["unanchored_components"]
