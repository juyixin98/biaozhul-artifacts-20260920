"""输入校验测试：违反模型的输入必须被拒绝。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.model import AnalysisRequest, TaskInput


def mk(tasks: list[dict], **kw) -> AnalysisRequest:
    return AnalysisRequest(tasks=[TaskInput(**x) for x in tasks], **kw)


def test_deadline_greater_than_period_rejected():
    with pytest.raises(ValidationError) as ei:
        mk([{"id": "a", "C": 3, "T": 8, "D": 10}])
    assert "D_i <= T_i" in str(ei.value)


def test_nonpositive_params_rejected():
    for bad in [
        {"id": "a", "C": 0, "T": 5, "D": 5},
        {"id": "a", "C": 1, "T": 0, "D": 0},
        {"id": "a", "C": 1, "T": 5, "D": -1},
    ]:
        with pytest.raises(ValidationError):
            mk([bad])


def test_negative_blocking_rejected():
    with pytest.raises(ValidationError):
        mk([{"id": "a", "C": 1, "T": 5, "D": 5, "B": -1}])


def test_duplicate_ids_rejected():
    with pytest.raises(ValidationError) as ei:
        mk(
            [
                {"id": "x", "C": 1, "T": 4, "D": 4},
                {"id": "x", "C": 1, "T": 8, "D": 8},
            ]
        )
    assert "重复" in str(ei.value)


def test_blank_id_rejected():
    with pytest.raises(ValidationError):
        mk([{"id": "   ", "C": 1, "T": 4, "D": 4}])


def test_empty_taskset_rejected():
    with pytest.raises(ValidationError):
        mk([])


def test_unknown_priority_policy_rejected():
    # 优先级规则固定为 RM；EDF/自定义优先级一律拒绝
    with pytest.raises(ValidationError):
        mk([{"id": "a", "C": 1, "T": 4, "D": 4}], priority_policy="EDF")


def test_extra_field_rejected():
    with pytest.raises(ValidationError):
        mk([{"id": "a", "C": 1, "T": 4, "D": 4, "priority": 0}])


def test_aliases_and_defaults_accepted():
    req = mk([{"id": "a", "C": 1, "T": 4, "D": 4}])  # B 缺省为 0
    assert req.tasks[0].blocking == 0
    assert req.priority_policy == "RM"
    req2 = AnalysisRequest.model_validate(
        {"tasks": [{"id": "a", "wcet": 1, "period": 4, "deadline": 4}]}
    )
    assert req2.tasks[0].wcet == 1


def test_oversized_value_rejected():
    with pytest.raises(ValidationError):
        mk([{"id": "a", "C": 1, "T": 10**9, "D": 10**9}])
