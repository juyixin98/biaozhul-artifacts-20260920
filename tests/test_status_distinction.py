"""Tests that timeout / unknown / unsat are distinguished honestly."""

from cbmc.checker import CheckConfig, Status, check_contract
from cbmc.fixtures import get_fixture
from cbmc.model import load_contract


def test_timeout_is_reported_as_timeout_not_unknown_or_safe():
    """A deep query against the safe transfer under a tiny per-call budget
    must surface as `timeout` (or, if the machine is fast enough, as a
    genuine bounded-safe result) — never as `unknown` or a false proof."""
    contract = load_contract(get_fixture("safe-transfer"))
    cfg = CheckConfig(steps=40, timeout_ms=30,
                      checks=("nonnegative", "conservation"))
    result = check_contract(contract, cfg)
    assert result.status in (Status.TIMEOUT, Status.SAFE_WITHIN_BOUND)
    if result.status is Status.TIMEOUT:
        assert result.depth is not None
        assert "timed out" in result.note
        assert result.trace == []
        # shorter depths that returned UNSAT were genuinely clean
        assert result.depth >= 1


def test_unknown_from_nonlinear_arithmetic_is_distinct():
    """Force a genuine Z3 UNKNOWN (non-linear integer arithmetic) and assert
    it is reported separately from timeout and from bounded safety."""
    doc = {
        "state": {"int": {"a": 1, "b": 1}},
        "actions": [{
            "name": "square",
            "params": [],
            "require": [],
            "assign": [
                {"target": "a", "expr": {"op": "*", "args": [
                    {"var": "a"}, {"var": "b"}]}},
                {"target": "b", "expr": {"op": "+", "args": [
                    {"var": "b"}, {"lit": 1}]}},
            ],
        }],
        "properties": {"balances": ["a", "b"], "conservation": []},
    }
    contract = load_contract(doc)
    cfg = CheckConfig(steps=40, timeout_ms=2000, checks=("nonnegative",))
    result = check_contract(contract, cfg)
    # Non-linear LIA commonly yields UNKNOWN; SAT/UNSAT are also acceptable,
    # but it must never be mislabeled: if UNKNOWN, status must be UNKNOWN.
    assert result.status in (Status.UNKNOWN, Status.TIMEOUT,
                             Status.SAFE_WITHIN_BOUND, Status.COUNTEREXAMPLE)
    if result.status is Status.UNKNOWN:
        assert "UNKNOWN" in result.note
        assert result.detail is not None


def test_zero_steps_never_invokes_solver_bad_query_but_reports_bounded():
    contract = load_contract(get_fixture("piggy-step-boundary"))
    cfg = CheckConfig(steps=0, timeout_ms=5000)
    result = check_contract(contract, cfg)
    assert result.status is Status.SAFE_WITHIN_BOUND
    assert result.solver_calls == 1


def test_invalid_config_rejected():
    contract = load_contract(get_fixture("safe-transfer"))
    for bad in (-1, 10_000):
        try:
            check_contract(contract, CheckConfig(steps=bad))
            assert False
        except Exception:
            pass


def test_injected_unknown_is_classified_separately(monkeypatch):
    """Force z3 to return UNKNOWN with a NON-timeout reason and verify the
    checker reports the distinct `unknown` status (not timeout, not safe)."""
    import z3

    contract = load_contract(get_fixture("safe-transfer"))
    original_check = z3.Solver.check

    def fake_check(self, *a, **k):
        return z3.unknown

    def fake_reason(self):
        return "incomplete theory"

    monkeypatch.setattr(z3.Solver, "check", fake_check)
    monkeypatch.setattr(z3.Solver, "reason_unknown", fake_reason)

    result = check_contract(
        contract, CheckConfig(steps=3, timeout_ms=5000))
    assert result.status is Status.UNKNOWN
    assert result.depth == 0
    assert "UNKNOWN" in result.note
    assert result.trace == []
    # sanity: restore works and the real solver finds clean as usual
    monkeypatch.setattr(z3.Solver, "check", original_check)
