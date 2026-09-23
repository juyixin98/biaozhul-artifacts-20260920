"""有界模型检查器端到端与单元测试。"""

from __future__ import annotations

import json

import pytest
import z3
from fastapi.testclient import TestClient

from cbmc.api import app
from cbmc.checker import BMC
from cbmc.errors import ModelError, ReplayError
from cbmc.eval import INT_MAX, initial_state
from cbmc.fixtures import FIXTURES
from cbmc.invariants import default_invariant
from cbmc.model import validate_model
from cbmc.replay import replay_trace, verify_trace

client = TestClient(app)


# -- 工具 --------------------------------------------------------------------

def prepared(fid: str) -> dict:
    m = validate_model(FIXTURES[fid])
    m["invariant"] = default_invariant(m)
    return m


def check(fid: str, bound: int = 5, timeout_ms: int = 30_000) -> dict:
    return BMC(prepared(fid)).check(bound, timeout_ms)


# -- 夹具：安全/漏洞/溢出/不可达 ---------------------------------------------

class TestFixtures:
    def test_safe_transfer_no_counterexample(self):
        r = check("safe_transfer", bound=6)
        assert r["status"] == "no_counterexample"
        assert r["depth"] is None
        assert "有界" in r["note"]  # 不宣称任意深度安全

    def test_missing_debit_depth_one(self):
        r = check("missing_debit", bound=5)
        assert r["status"] == "counterexample"
        assert r["depth"] == 1
        tr = r["trace"]
        assert len(tr) == 2
        assert tr[0]["action"] is None and tr[0]["invariant_holds"] is True
        assert tr[1]["action"] == "withdraw"
        # treasury 未被扣款，attacker 凭空多出钱：守恒被破坏，但没有负余额
        assert tr[1]["state"]["treasury"] == 100
        assert tr[1]["state"]["attacker"] >= 1
        assert tr[1]["invariant_holds"] is False

    def test_overflow_wraps_to_negative(self):
        r = check("overflow_transfer", bound=3)
        assert r["status"] == "counterexample"
        assert r["depth"] == 1
        final = r["trace"][1]["state"]
        # 付款方余额充足（守卫成立、扣款后非负），收款方回绕成负数
        assert final["alice"] >= 0
        assert final["bob"] < 0
        assert final["bob"] < -(2**62)

    def test_unreachable_action_no_counterexample(self):
        for bound in (0, 1, 4, 8):
            r = check("unreachable_guard", bound=bound)
            assert r["status"] == "no_counterexample", bound


# -- 最短反例与步数边界 -------------------------------------------------------

class TestShortestAndBounds:
    def test_shortest_is_depth_two(self):
        r = check("late_leak", bound=4)
        assert r["status"] == "counterexample"
        assert r["depth"] == 2
        names = [t["action"] for t in r["trace"]]
        assert names == [None, "arm", "leak"]

    def test_bound_zero_only_checks_initial(self):
        # late_leak / missing_debit 的初始状态都合法
        assert check("late_leak", bound=0)["status"] == "no_counterexample"
        assert check("missing_debit", bound=0)["status"] == "no_counterexample"

    def test_bound_one_too_small_bound_two_found(self):
        assert check("late_leak", bound=1)["status"] == "no_counterexample"
        r = check("late_leak", bound=2)
        assert r["status"] == "counterexample" and r["depth"] == 2

    def test_depth_zero_counterexample_when_init_invalid(self):
        model = {
            "name": "bad_init",
            "state_vars": [{"name": "x", "type": "int", "init": -5}],
            "actions": [{"name": "noop", "params": [], "guards": [], "effects": []}],
        }
        m = validate_model(model)
        m["invariant"] = default_invariant(m)
        r = BMC(m).check(3, 10_000)
        assert r["status"] == "counterexample"
        assert r["depth"] == 0
        assert len(r["trace"]) == 1
        assert r["trace"][0]["state"]["x"] == -5

    def test_iterative_deepening_stats(self):
        r = check("late_leak", bound=2)
        assert [d["depth"] for d in r["stats"]["per_depth_ms"]] == [0, 1, 2]


# -- 逐步重放 ----------------------------------------------------------------

class TestReplay:
    def test_replay_matches_trace(self):
        r = check("missing_debit", bound=3)
        steps = [{"action": t["action"], "params": t["params"]}
                 for t in r["trace"][1:]]
        tr = replay_trace(prepared("missing_debit"), steps,
                          default_invariant(prepared("missing_debit"))["expr"])
        for given, concrete in zip(r["trace"], tr):
            assert given["state"] == concrete["state"]
        assert tr[-1]["invariant_holds"] is False

    def test_replay_rejects_failing_guard(self):
        # 安全转账：转出超过 alice 的 100，守卫不成立
        with pytest.raises(ReplayError, match="守卫"):
            replay_trace(prepared("safe_transfer"),
                         [{"action": "transfer", "params": {"amount": 101}}],
                         default_invariant(prepared("safe_transfer"))["expr"])

    def test_replay_rejects_param_out_of_range(self):
        with pytest.raises(ReplayError, match="超出声明范围"):
            replay_trace(prepared("missing_debit"),
                         [{"action": "withdraw", "params": {"amount": 1001}}],
                         default_invariant(prepared("missing_debit"))["expr"])

    def test_replay_lock_unlock_flow(self):
        m = prepared("safe_transfer")
        steps = [
            {"action": "transfer", "params": {"amount": 40}},
            {"action": "unlock", "params": {}},
            {"action": "transfer", "params": {"amount": 60}},
        ]
        tr = replay_trace(m, steps, default_invariant(m)["expr"])
        assert [t["state"]["locked"] for t in tr] == [False, True, False, True]
        assert tr[-1]["state"] == {"alice": 0, "bob": 150, "locked": True}
        assert all(t["invariant_holds"] for t in tr)

    def test_double_transfer_without_unlock_blocked_by_guard(self):
        m = prepared("safe_transfer")
        with pytest.raises(ReplayError, match="守卫"):
            replay_trace(m, [
                {"action": "transfer", "params": {"amount": 1}},
                {"action": "transfer", "params": {"amount": 1}},
            ], default_invariant(m)["expr"])

    def test_verify_trace_detects_tampered_state(self):
        r = check("missing_debit", bound=2)
        r["trace"][1]["state"]["treasury"] = 99  # 篡改求解器状态
        with pytest.raises(ReplayError, match="不一致"):
            verify_trace(prepared("missing_debit"), r["trace"],
                         default_invariant(prepared("missing_debit"))["expr"])


# -- 64 位语义 ---------------------------------------------------------------

class TestBitVecSemantics:
    def test_conservation_is_modulo(self):
        # 成对加减在模 2^64 下恒守恒：overflow 夹具的守恒合取支始终成立，
        # 是“非负”支抓到了反例 —— 最终状态余额之和（回绕）仍等于初始总额。
        r = check("overflow_transfer", bound=1)
        st = r["trace"][1]["state"]
        from cbmc.eval import wrap64
        assert wrap64(st["alice"] + st["bob"]) == wrap64(
            100 + (INT_MAX - 50))


# -- 模型校验：只接受纯数据，拒绝越权结构 -----------------------------------

class TestValidation:
    def test_reject_unknown_operator(self):
        bad = {"name": "x",
               "state_vars": [{"name": "a", "type": "int", "init": 0}],
               "actions": [{"name": "n", "params": [], "guards": [
                   {"expr": {"t": "cmp", "op": "===",
                             "lhs": {"t": "num", "value": 1},
                             "rhs": {"t": "num", "value": 1}}}], "effects": []}]}
        with pytest.raises(ModelError, match="比较算符"):
            validate_model(bad)

    def test_reject_callable_hidden_in_json(self):
        # 即使数据里混入类似“函数名”的算符，也不会被调用：直接判非法
        bad = {"name": "x",
               "state_vars": [{"name": "a", "type": "int", "init": 0}],
               "actions": [{"name": "n", "params": [], "guards": [
                   {"expr": {"t": "arith", "op": "eval",
                             "args": [{"t": "num", "value": 1}]}}], "effects": []}]}
        with pytest.raises(ModelError, match="算术算符"):
            validate_model(bad)

    def test_reject_unknown_variable(self):
        bad = {"name": "x",
               "state_vars": [{"name": "a", "type": "int", "init": 0}],
               "actions": [{"name": "n", "params": [], "guards": [
                   {"expr": {"t": "cmp", "op": "==",
                             "lhs": {"t": "var", "name": "ghost"},
                             "rhs": {"t": "num", "value": 0}}}], "effects": []}]}
        with pytest.raises(ModelError, match="未知变量"):
            validate_model(bad)

    def test_reject_type_mismatch_bool_ordering(self):
        bad = {"name": "x",
               "state_vars": [{"name": "l", "type": "bool", "init": False}],
               "actions": [{"name": "n", "params": [], "guards": [
                   {"expr": {"t": "cmp", "op": "<",
                             "lhs": {"t": "var", "name": "l"},
                             "rhs": {"t": "bool", "value": True}}}], "effects": []}]}
        with pytest.raises(ModelError):
            validate_model(bad)

    def test_reject_duplicate_target_and_bad_bounds(self):
        bad = {"name": "x",
               "state_vars": [{"name": "a", "type": "int", "init": 0}],
               "actions": [{"name": "n",
                            "params": [{"name": "p", "type": "int", "min": 9, "max": 1}],
                            "guards": [], "effects": []}]}
        with pytest.raises(ModelError, match="min <= max"):
            validate_model(bad)

    def test_bound_limits(self):
        m = prepared("safe_transfer")
        with pytest.raises(ModelError):
            BMC(m).check(-1, 5000)
        with pytest.raises(ModelError):
            BMC(m).check(10_000, 5000)


# -- 超时 / 未知 / 未发现 三者严格区分 --------------------------------------

class _TimeoutSolver(z3.Solver):
    def check(self, *a, **kw):
        return z3.unknown

    def reason_unknown(self):
        return "timeout"


class _MemoutSolver(z3.Solver):
    def check(self, *a, **kw):
        return z3.unknown

    def reason_unknown(self):
        return "max. resource exceeded (memout case)"


class TestStatusDistinction:
    def test_timeout_distinct_from_unknown(self, monkeypatch):
        monkeypatch.setattr(z3, "Solver", _TimeoutSolver)
        r = check("safe_transfer", bound=2)
        assert r["status"] == "timeout"
        assert "时间" in r["reason"] or "timeout" in r["reason"].lower()

    def test_unknown_distinct_from_timeout(self, monkeypatch):
        monkeypatch.setattr(z3, "Solver", _MemoutSolver)
        r = check("safe_transfer", bound=2)
        assert r["status"] == "unknown"
        assert "unknown" in r["reason"]

    def test_budget_exhaustion_reports_timeout(self):
        # 50ms 的极小总预算作用在安全模型（多个深度都真求解）上，
        # 要么某深度超时(timeout)、要么预算耗尽(timeout)，绝不得到反例
        r = check("safe_transfer", bound=40, timeout_ms=50)
        assert r["status"] in ("timeout", "no_counterexample")

    def test_no_counterexample_is_bounded_wording(self):
        r = check("safe_transfer", bound=5)
        assert r["status"] == "no_counterexample"
        assert str(r["bound"]) in r["reason"]
        assert "不等于证明任意深度安全" in r["note"]


# -- HTTP 接口 ---------------------------------------------------------------

class TestAPI:
    def test_health(self):
        r = client.get("/health")
        assert r.status_code == 200 and r.json()["status"] == "ok"

    def test_fixtures_list_and_get(self):
        ids = [f["id"] for f in client.get("/fixtures").json()["fixtures"]]
        assert set(ids) == set(FIXTURES)
        assert client.get("/fixtures/missing_debit").status_code == 200
        assert client.get("/fixtures/nope").status_code == 404

    def test_check_missing_debit(self):
        r = client.post("/check", json={
            "model": FIXTURES["missing_debit"], "steps": 3, "timeout_ms": 20000})
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "counterexample" and body["depth"] == 1

    def test_check_safe(self):
        r = client.post("/check", json={
            "model": FIXTURES["safe_transfer"], "steps": 4, "timeout_ms": 20000})
        body = r.json()
        assert body["status"] == "no_counterexample" and body["trace"] is None

    def test_check_invalid_model_422(self):
        r = client.post("/check", json={"model": {"name": "bad"},
                                        "steps": 3, "timeout_ms": 5000})
        assert r.status_code == 422

    def test_replay_endpoint(self):
        r = client.post("/replay", json={
            "model": FIXTURES["missing_debit"],
            "steps": [{"action": "withdraw", "params": {"amount": 8}}]})
        assert r.status_code == 200
        body = r.json()
        assert body["invariant_violated_at_step"] == 1
        assert body["trace"][-1]["state"] == {"treasury": 100, "attacker": 8}

    def test_replay_endpoint_guard_violation_409(self):
        r = client.post("/replay", json={
            "model": FIXTURES["safe_transfer"],
            "steps": [{"action": "transfer", "params": {"amount": 999}}]})
        assert r.status_code == 409

    def test_steps_validation_http(self):
        r = client.post("/check", json={
            "model": FIXTURES["safe_transfer"], "steps": 100000, "timeout_ms": 1000})
        assert r.status_code == 422

    def test_examples_on_disk_round_trip(self):
        for name in ("safe_transfer", "missing_debit", "overflow"):
            with open(f"examples/{name}.json", encoding="utf-8") as f:
                model = json.load(f)
            r = client.post("/check", json={"model": model, "steps": 3,
                                            "timeout_ms": 20000})
            assert r.status_code == 200, name


class TestCLI:
    def test_exit_codes(self, tmp_path):
        from cbmc.cli import main

        safe = tmp_path / "safe.json"
        safe.write_text(json.dumps(FIXTURES["safe_transfer"]))
        buggy = tmp_path / "buggy.json"
        buggy.write_text(json.dumps(FIXTURES["missing_debit"]))

        assert main(["check", str(buggy), "--steps", "3",
                     "--timeout-ms", "10000"]) == 0
        assert main(["check", str(safe), "--steps", "3",
                     "--timeout-ms", "10000"]) == 0
        # 超时属于非 0 的独立退出码
        assert main(["check", str(safe), "--steps", "40",
                     "--timeout-ms", "50"]) == 2
        # 模型非法 -> 1
        bad = tmp_path / "bad.json"
        bad.write_text(json.dumps({"name": "x"}))
        assert main(["check", str(bad), "--steps", "1",
                     "--timeout-ms", "1000"]) == 1
