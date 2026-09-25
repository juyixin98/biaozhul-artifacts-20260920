"""DAG 构建与校验测试。"""

from __future__ import annotations

import pytest

from tensor_planner.dag import DAGBuilder, DAGValidationError


def test_simple_add_shape_inference() -> None:
    dag = (
        DAGBuilder()
        .add_input("A", (2, 3))
        .add_input("B", (2, 3))
        .add_add("C", "A", "B", time=1)
        .build()
    )
    assert dag.shapes["C"] == (2, 3)
    assert dag.is_output("C")
    assert dag.last_use("A") == 1  # A 只在 time=1 被读取
    assert dag.last_use("C") == dag.horizon  # 输出存活到程序结束


def test_add_shape_mismatch_rejected() -> None:
    b = DAGBuilder().add_input("A", (2, 2)).add_input("B", (2, 3))
    with pytest.raises(DAGValidationError, match="形状不一致"):
        b.add_add("C", "A", "B", time=1)


def test_matmul_shape_and_contraction() -> None:
    dag = (
        DAGBuilder()
        .add_input("A", (2, 3))
        .add_input("B", (3, 4))
        .add_matmul("C", "A", "B", time=1)
        .build()
    )
    assert dag.shapes["C"] == (2, 4)
    assert dag.num_elements("C") == 8


def test_matmul_inner_dimension_mismatch() -> None:
    b = DAGBuilder().add_input("A", (2, 3)).add_input("B", (4, 2))
    with pytest.raises(DAGValidationError, match="内维不匹配"):
        b.add_matmul("C", "A", "B", time=1)


def test_matmul_requires_2d() -> None:
    b = DAGBuilder().add_input("A", (2, 3)).add_input("B", (3,))
    with pytest.raises(DAGValidationError, match="二维"):
        b.add_matmul("C", "A", "B", time=1)


def test_duplicate_name_rejected() -> None:
    b = DAGBuilder().add_input("A", (2, 2))
    with pytest.raises(DAGValidationError, match="值名重复"):
        b.add_input("A", (2, 2))


def test_undefined_reference_rejected() -> None:
    b = DAGBuilder()
    with pytest.raises(DAGValidationError, match="未定义"):
        b.add_add("C", "A", "B", time=1)


def test_backward_edge_rejected() -> None:
    b = (
        DAGBuilder()
        .add_input("X", (2, 2))
        .add_add("C", "X", "X", time=2)
        .add_add("D", "X", "C", time=1)  # time 1 引用 time 2 的值
    )
    with pytest.raises(DAGValidationError, match="从早指向晚"):
        b.build()


def test_two_ops_same_time_rejected() -> None:
    b = (
        DAGBuilder()
        .add_input("A", (2, 2))
        .add_input("B", (2, 2))
        .add_add("C", "A", "B", time=1)
        .add_add("D", "A", "C", time=1)
    )
    with pytest.raises(DAGValidationError, match="每个时刻至多一个 op"):
        b.build()


def test_time_must_be_positive() -> None:
    with pytest.raises(DAGValidationError, match=">= 1"):
        DAGBuilder().add_input("A", (2,)).add_add("C", "A", "A", time=0).build()


def test_zero_or_negative_dimension_rejected() -> None:
    with pytest.raises(DAGValidationError, match="正整数"):
        DAGBuilder().add_input("A", (0, 2)).build()


def test_slice_out_of_bounds_rejected() -> None:
    b = (
        DAGBuilder()
        .add_input("X", (4, 4))
        .add_slice("v", "X", [0, 0], [3, 4], time=1)
        .add_slice("w", "X", [2, 0], [2, 4], time=2)  # 行 2 与 v 重叠
    )
    with pytest.raises(DAGValidationError, match="重叠"):
        b.build()


def test_slice_partition_gap_rejected() -> None:
    b = (
        DAGBuilder()
        .add_input("X", (4,))
        .add_slice("v", "X", [0], [2], time=1)
        # 缺 [2:4)
    )
    with pytest.raises(DAGValidationError, match="完整划分"):
        b.build()


def test_slice_valid_partition() -> None:
    dag = (
        DAGBuilder()
        .add_input("X", (4, 4))
        .add_slice("v", "X", [0, 0], [2, 4], time=1)
        .add_slice("w", "X", [2, 0], [2, 4], time=2)
        .build()
    )
    assert dag.root_of["v"] == "X"
    assert dag.root_of["w"] == "X"
    assert dag.root_of["X"] == "X"
    assert dag.view_offset("v") == 0
    assert dag.view_offset("w") == 8  # 2*4 C 布局偏移


def test_nested_slice_rejected() -> None:
    b = (
        DAGBuilder()
        .add_input("X", (4,))
        .add_slice("v", "X", [0], [2], time=1)
        .add_slice("w", "X", [2], [2], time=2)
        .add_slice("z", "v", [0], [1], time=3)
    )
    with pytest.raises(DAGValidationError, match="单级别名"):
        b.build()


def test_slice_wrong_ndim_rejected() -> None:
    b = DAGBuilder().add_input("X", (4, 4))
    with pytest.raises(DAGValidationError, match="维度数"):
        b.add_slice("v", "X", [0], [2], time=1)
