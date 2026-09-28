"""补充契约测试：剩余算子、环检测、日志回放、配置。"""

from __future__ import annotations

import numpy as np
import pytest

from recomp_scheduler.config import Config, to_bytes
from recomp_scheduler.engine import execute
from recomp_scheduler.errors import GraphValidationError
from recomp_scheduler.graph import build_graph
from recomp_scheduler.journal import Journal, new_run_id
from recomp_scheduler.memory import simulate
from recomp_scheduler.planner import make_plan
from recomp_scheduler.state import TrainState
from recomp_scheduler.verification import finite_difference_check


def _mul_graph():
    # y = linear( tanh(a) * relu(b) )，a/b 来自两个输入，覆盖 mul 两路梯度。
    raw = [
        {"id": "a", "op": "input", "attrs": {"shape": [2, 3]}},
        {"id": "b", "op": "input", "attrs": {"shape": [2, 3]}},
        {"id": "ta", "op": "tanh", "inputs": ["a"]},
        {"id": "rb", "op": "relu", "inputs": ["b"]},
        {"id": "p", "op": "mul", "inputs": ["ta", "rb"]},
        {"id": "y", "op": "linear", "inputs": ["p"], "attrs": {"out_features": 2}},
    ]
    return build_graph(raw, outputs=["y"])


def test_mul_op_gradients_by_finite_difference() -> None:
    g = _mul_graph()
    plan = make_plan((), len(g.order))
    state = TrainState.synthetic(g, seed=11)
    result = execute(state, plan, predicted_peak=simulate(g, plan).peak_elements)
    report = finite_difference_check(
        g,
        state.inputs,
        state.params,
        state.grad_outputs,
        11,
        result.input_grads,
        result.param_grads,
    )
    assert report.passed, report.per_component_max_err
    assert report.max_abs_err < 1e-6
    # 两个输入都必须有梯度（mul 的两路）。
    assert set(result.input_grads) == {"a", "b"}


def test_cycle_detected_as_input_error() -> None:
    # 直接构造一个引用环（绕过拓扑序列表的"只能引用先前节点"限制，
    # 以确保 DFS 环检测本身被覆盖）。
    g_linear = build_graph(
        [
            {"id": "x", "op": "input", "attrs": {"shape": [2]}},
            {"id": "u", "op": "relu", "inputs": ["x"]},
            {"id": "v", "op": "relu", "inputs": ["u"]},
        ],
        outputs=["v"],
    )
    # 人为制造环：v -> u -> v。
    g_linear.nodes["u"].inputs = ("v",)
    from recomp_scheduler.graph import _validate_acyclic

    with pytest.raises(GraphValidationError) as exc:
        _validate_acyclic(g_linear.nodes, g_linear.order)
    assert exc.value.category == "input_error"


def test_journal_records_and_replays(tmp_path) -> None:
    journal = Journal(str(tmp_path / "logs"))
    rid = journal.write(
        {
            "kind": "execute",
            "seed": 7,
            "peak_match": True,
            "reason": "synthetic replay record",
            "nested": {"x": np.int64(3)},
        }
    )
    loaded = journal.load(rid)
    assert loaded["run_id"] == rid
    assert loaded["seed"] == 7
    assert loaded["peak_match"] is True
    assert loaded["nested"]["x"] == 3  # numpy 标量被 JSON 安全化
    # run_id 唯一且可排序。
    assert new_run_id() != rid


def test_journal_records_error_with_category(tmp_path) -> None:
    from recomp_scheduler.errors import BudgetInfeasibleError

    journal = Journal(str(tmp_path / "logs"))
    err = BudgetInfeasibleError("boom", phase="planning", budget=1)
    rid = journal.record_error(err, context={"k": 1})
    rec = journal.load(rid)
    assert rec["kind"] == "error"
    assert rec["error"]["category"] == "resource_exhausted"
    assert rec["context"] == {"k": 1}


def test_config_element_bytes_and_conversion() -> None:
    cfg = Config()
    assert cfg.element_bytes() == 8  # float64
    assert to_bytes(10, "float64") == 80
    with pytest.raises(GraphValidationError):
        Config(dtype="float16").element_bytes()


def test_unknown_run_id_keyerror(tmp_path) -> None:
    journal = Journal(str(tmp_path / "logs"))
    with pytest.raises(KeyError):
        journal.load("missing")
