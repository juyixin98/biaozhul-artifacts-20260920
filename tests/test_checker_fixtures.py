"""BMC property tests across all bundled fixtures (real Z3 solving)."""


from cbmc.checker import CheckConfig, Status, check_contract
from cbmc.errors import ReplayError
from cbmc.fixtures import list_fixtures, load_example
from cbmc.replay import replay_trace


def test_all_fixtures_load():
    ids = [f["id"] for f in list_fixtures()]
    assert set(ids) == {
        "safe-transfer", "escrow-missing-debit", "uint8-pool-overflow",
        "unreachable-target", "piggy-step-boundary",
    }
    for fid in ids:
        assert load_example(fid).name


# --------------------------------------------------------------------------- #
# safe transfer
# --------------------------------------------------------------------------- #

def test_safe_transfer_is_clean_within_bound(run):
    _, result, report = run("safe-transfer", steps=20, timeout_ms=15_000)
    assert result.status is Status.SAFE_WITHIN_BOUND
    assert result.trace == []
    assert report is None
    # the result must be explicitly bounded, never a proof of safety
    assert "NOT a proof" in result.note or "Bounded" in result.note


def test_safe_transfer_depth_zero_is_clean(run):
    _, result, _ = run("safe-transfer", steps=0)
    assert result.status is Status.SAFE_WITHIN_BOUND


# --------------------------------------------------------------------------- #
# missing debit
# --------------------------------------------------------------------------- #

def test_missing_debit_shortest_counterexample_depth_2(run):
    _, result, report = run("escrow-missing-debit", steps=6)
    assert result.status is Status.COUNTEREXAMPLE
    assert result.depth == 2                      # shortest, not just any
    assert result.violation == "conservation:0"
    assert [s.action for s in result.trace if s.action] == ["deposit", "settle"]


def test_missing_debit_below_true_depth_reports_safe(run):
    _, result, _ = run("escrow-missing-debit", steps=1)
    assert result.status is Status.SAFE_WITHIN_BOUND


def test_missing_debit_replay_executes_and_confirms(run):
    contract, result, report = run("escrow-missing-debit", steps=4)
    assert report.executable is True
    assert report.states_match_solver is True
    # independent concrete evaluation: settle credited seller without
    # debiting escrow, so the total exceeds the initial 10 by exactly the
    # (still-full) escrow amount.
    final = report.final_state
    escrow = final["escrow"]
    assert escrow > 0
    assert sum(final[v] for v in ("buyer", "seller", "escrow")) == 10 + escrow
    assert "conservation:0" in report.violated


def test_missing_debit_nonneg_only_has_no_counterexample(run):
    _, result, _ = run("escrow-missing-debit", steps=4,
                       checks=["nonnegative"])
    # the bug mints money; it never makes a balance negative
    assert result.status is Status.SAFE_WITHIN_BOUND


# --------------------------------------------------------------------------- #
# uint8 overflow
# --------------------------------------------------------------------------- #

def test_uint8_overflow_wraps_and_breaks_conservation(run):
    _, result, report = run("uint8-pool-overflow", steps=3)
    assert result.status is Status.COUNTEREXAMPLE
    assert result.depth == 1
    assert result.violation == "conservation:0"
    final = result.trace[-1].state
    # 240 + amount wrapped modulo 256, so vault is now a small wrapped value
    assert final["vault"] < 240
    assert "conservation:0" in report.violated
    # unsigned balances are never reported "negative"
    assert not any(v.property.startswith("nonnegative") and not v.holds
                   for v in report.property_verdicts)


def test_uint8_step0_is_clean(run):
    _, result, _ = run("uint8-pool-overflow", steps=0)
    assert result.status is Status.SAFE_WITHIN_BOUND


def test_math_int_version_of_overflow_does_not_wrap():
    """Same model with math integers must NOT exhibit the wrap: the
    conservation sum of a uint group catches exactly machine wrap."""
    from cbmc.model import load_contract

    doc = {
        "state": {"int": {"vault": 240, "staging": 20}},
        "actions": [{
            "name": "add",
            "params": [{"name": "amount", "type": "int", "min": 1, "max": 20}],
            "require": [{"op": "<=", "args": [{"var": "amount"},
                                              {"var": "staging"}]}],
            "assign": [
                {"target": "vault",
                 "expr": {"op": "+", "args": [{"var": "vault"},
                                              {"var": "amount"}]}},
                {"target": "staging",
                 "expr": {"op": "-", "args": [{"var": "staging"},
                                              {"var": "amount"}]}},
            ],
        }],
        "properties": {"balances": ["vault", "staging"],
                       "conservation": [["vault", "staging"]]},
    }
    result = check_contract(load_contract(doc),
                            CheckConfig(steps=3, timeout_ms=5000))
    assert result.status is Status.SAFE_WITHIN_BOUND


# --------------------------------------------------------------------------- #
# unreachable target
# --------------------------------------------------------------------------- #

def test_unreachable_target_is_unsat_within_bound():
    contract = load_example("unreachable-target")
    cfg = CheckConfig(steps=8, timeout_ms=5000, checks=("targets",))
    # disable the reachable target by checking the unsatisfiable one alone:
    # do this by mutating properties to keep only the impossible target.
    contract.raw["properties"]["targets"] = [
        t for t in contract.raw["properties"]["targets"]
        if t["name"] == "drained_and_funded"
    ]
    from cbmc.model import load_contract
    c2 = load_contract(contract.raw)
    result = check_contract(c2, cfg)
    assert result.status is Status.SAFE_WITHIN_BOUND
    assert result.trace == []


def test_reachable_target_found_in_one_step(run):
    contract, result, report = run("unreachable-target", steps=8)
    assert result.status is Status.COUNTEREXAMPLE
    assert result.depth == 1
    assert result.violation == "target:cashier_holds_everything"
    assert report.final_state["cashier"] == 10


# --------------------------------------------------------------------------- #
# step boundary
# --------------------------------------------------------------------------- #

def test_piggy_boundary_exact_shortest_depth(run):
    # smashed requires exactly 15 deposits; at steps=14 unreachable, at 15/16
    # the shortest counterexample has depth 16 (15 deposits + 1 smash).
    _, too_small, _ = run("piggy-step-boundary", steps=15, timeout_ms=10_000)
    assert too_small.status is Status.SAFE_WITHIN_BOUND

    _, exact, report = run("piggy-step-boundary", steps=16, timeout_ms=10_000)
    assert exact.status is Status.COUNTEREXAMPLE
    assert exact.depth == 16
    actions = [s.action for s in exact.trace if s.action]
    assert actions[:15] == ["deposit"] * 15 and actions[15] == "smash"
    assert "target:smashed" in report.violated


# --------------------------------------------------------------------------- #
# replay must reject invalid / tampered traces
# --------------------------------------------------------------------------- #

def test_replay_rejects_action_under_false_guard():
    contract = load_example("escrow-missing-debit")
    # settle directly from the initial state is disabled (locked=false)
    trace = [
        {"step": 0, "action": None, "params": {},
         "state": {"buyer": 10, "seller": 0, "escrow": 0, "locked": False}},
        {"step": 1, "action": "settle", "params": {},
         "state": {"buyer": 10, "seller": 10, "escrow": 10, "locked": False}},
    ]
    try:
        replay_trace(contract, trace)
        assert False, "expected ReplayError"
    except ReplayError as exc:
        assert "disabled" in str(exc)


def test_replay_rejects_state_mismatch():
    contract = load_example("escrow-missing-debit")
    trace = [
        {"step": 0, "action": None, "params": {},
         "state": {"buyer": 10, "seller": 0, "escrow": 0, "locked": False}},
        {"step": 1, "action": "deposit", "params": {"amount": 10},
         "state": {"buyer": 999, "seller": 0, "escrow": 10, "locked": True}},
    ]
    try:
        replay_trace(contract, trace)
        assert False
    except ReplayError as exc:
        assert "mismatch" in str(exc)


def test_replay_empty_trace_errors():
    contract = load_example("safe-transfer")
    try:
        replay_trace(contract, [])
        assert False
    except ReplayError:
        pass
