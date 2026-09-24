"""Tests for input/model validation: violations must be rejected."""

import pytest
from pydantic import ValidationError

from app.models import AnalysisRequest


def make_task(**over):
    base = {"id": "T", "wcet": 1, "period": 4, "deadline": 4, "priority": 1}
    base.update(over)
    return base


class TestRejectModelViolations:
    def test_deadline_greater_than_period_rejected(self):
        with pytest.raises(ValidationError) as ei:
            AnalysisRequest.model_validate({"tasks": [make_task(deadline=5, period=4)]})
        assert "D <= T" in str(ei.value) or "deadline" in str(ei.value)

    def test_equal_priorities_rejected(self):
        with pytest.raises(ValidationError) as ei:
            AnalysisRequest.model_validate(
                {"tasks": [make_task(id="A", priority=2), make_task(id="B", priority=2)]}
            )
        assert "equal priorities" in str(ei.value)

    def test_duplicate_ids_rejected(self):
        with pytest.raises(ValidationError) as ei:
            AnalysisRequest.model_validate(
                {"tasks": [make_task(id="X", priority=1), make_task(id="X", priority=2)]}
            )
        assert "duplicate task ids" in str(ei.value)

    def test_empty_taskset_rejected(self):
        with pytest.raises(ValidationError):
            AnalysisRequest.model_validate({"tasks": []})

    def test_negative_wcet_rejected(self):
        with pytest.raises(ValidationError):
            AnalysisRequest.model_validate({"tasks": [make_task(wcet=-1)]})

    def test_zero_period_rejected(self):
        with pytest.raises(ValidationError):
            AnalysisRequest.model_validate({"tasks": [make_task(period=0)]})

    def test_negative_blocking_rejected(self):
        with pytest.raises(ValidationError):
            AnalysisRequest.model_validate({"tasks": [make_task(blocking=-1)]})

    def test_priority_zero_rejected(self):
        with pytest.raises(ValidationError):
            AnalysisRequest.model_validate({"tasks": [make_task(priority=0)]})

    def test_blank_id_rejected(self):
        with pytest.raises(ValidationError):
            AnalysisRequest.model_validate({"tasks": [make_task(id="   ")]})

    def test_bad_id_charset_rejected(self):
        with pytest.raises(ValidationError):
            AnalysisRequest.model_validate({"tasks": [make_task(id="bad id!")]})

    def test_non_integer_rejected(self):
        with pytest.raises(ValidationError):
            AnalysisRequest.model_validate({"tasks": [make_task(wcet=1.5)]})

    def test_zero_wcet_allowed(self):
        req = AnalysisRequest.model_validate({"tasks": [make_task(wcet=0, deadline=0)]})
        assert req.tasks[0].wcet == 0

    def test_accepts_valid_constrained_set(self):
        req = AnalysisRequest.model_validate(
            {"tasks": [
                make_task(id="A", priority=1),
                make_task(id="B", wcet=2, period=9, deadline=7, priority=2, blocking=1),
            ]}
        )
        assert len(req.tasks) == 2
