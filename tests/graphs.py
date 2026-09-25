"""测试与示例共用的 DAG 构造器。"""

from __future__ import annotations

from tensor_planner.dag import DAGBuilder


def chain_dag():
    """A,B(4) -> C=A+B -> D=C+B：最简单的复用链。"""
    return (
        DAGBuilder()
        .add_input("A", (4,))
        .add_input("B", (4,))
        .add_add("C", "A", "B", time=1)
        .add_add("D", "C", "B", time=2)
        .build()
    )


def multi_consumer_dag():
    """A 同时被 t1 与 t3 消费，存活区间拉长，阻止早期复用。"""
    return (
        DAGBuilder()
        .add_input("A", (8,))
        .add_input("B", (8,))
        .add_add("C", "A", "B", time=1)
        .add_add("D", "B", "C", time=2)
        .add_add("E", "A", "D", time=3)
        .build()
    )


def branch_join_dag():
    """P 扇出到两个分支 A、B，t3 汇合：C=A+B。"""
    return (
        DAGBuilder()
        .add_input("P", (6,))
        .add_input("Q", (6,))
        .add_input("R", (6,))
        .add_add("A", "P", "Q", time=1)
        .add_add("B", "P", "R", time=2)
        .add_add("C", "A", "B", time=3)
        .build()
    )


def shared_view_dag():
    """X(4x4) 划分为两个行切片视图 v、w，分别与 P 做矩阵乘。"""
    return (
        DAGBuilder()
        .add_input("X", (4, 4))
        .add_input("P", (4, 3))
        .add_slice("v", "X", [0, 0], [2, 4], time=1)
        .add_slice("w", "X", [2, 0], [2, 4], time=2)
        .add_matmul("A", "v", "P", time=3)
        .add_matmul("B", "w", "P", time=4)
        .build()
    )
