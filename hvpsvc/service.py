"""Service engine: orchestrates state, AD core, independent verification and
run logging. This is the single place where the HTTP layer meets the core.
"""

from __future__ import annotations

from typing import Any

import numpy as np

from . import autodiff as ad
from .errors import HVPError, InputError, NonSmoothError
from .graph import NonsmoothConfig
from .runs import RunLogger
from .state import StateStore
from .validation import (
    MpmathReference,
    check_gradient_against_ref,
    check_vector_against_ref,
    finite_difference_gradient,
)


class HVPService:
    def __init__(self, store: StateStore | None = None,
                 logger: RunLogger | None = None) -> None:
        self.store = store or StateStore()
        self.logger = logger or RunLogger()

    def _fail(self, run_id: str, operation: str, state_id, exc: HVPError,
              checks=None) -> None:
        exc.run_id = run_id
        self.logger.record(run_id, operation, state_id=state_id,
                           status="error", checks=checks or [],
                           error=exc.to_dict())
        raise exc

    # ------------------------------------------------------------------
    def create_function(self, payload: dict[str, Any]) -> dict[str, Any]:
        run_id = self.logger.new_run_id()
        try:
            ns = NonsmoothConfig(
                policy=payload.get("nonsmooth", {}).get("policy", "reject"),
                subgradient=float(
                    payload.get("nonsmooth", {}).get("subgradient", 0.0)),
            )
            budget = payload.get("budget", {}) or {}
            state = self.store.create(
                payload,
                nonsmooth=ns,
                budget_limits={
                    "max_nodes": int(budget.get("max_nodes", 200_000)),
                    "max_evals": int(budget.get("max_evals", 2_000_000)),
                },
            )
            entry = self.logger.record(
                run_id, "create_function", state_id=state.state_id,
                status="ok",
                request_summary={"variables": state.layout.names(),
                                 "total_size": state.layout.total_size,
                                 "nodes": len(state.graph.order)},
                result_summary={"state_id": state.state_id,
                                "version": state.version})
            return {"state_id": state.state_id, "version": state.version,
                    "total_size": state.layout.total_size, "run_id": run_id}
        except HVPError as exc:
            self._fail(run_id, "create_function", None, exc)

    def set_point(self, state_id: str, point: dict[str, Any],
                  expected_version: int | None = None) -> dict[str, Any]:
        run_id = self.logger.new_run_id()
        state = self.store.get(state_id)  # StateConflictError if unknown
        try:
            version = state.set_point(point, expected_version=expected_version)
            # eagerly validate the forward pass / domain at the new point
            budget = state.new_budget()
            value = state.graph.evaluate(
                [state.output], state.point, state.layout,
                budget=budget, stage="forward")[0]
            self.logger.record(
                run_id, "set_point", state_id=state_id, status="ok",
                request_summary={"expected_version": expected_version},
                intermediates={"version": version,
                               "forward_value": float(value),
                               "budget_evals": budget.evals_used},
                result_summary={"version": version, "value": float(value)})
            return {"version": version, "value": float(value),
                    "run_id": run_id}
        except HVPError as exc:
            self._fail(run_id, "set_point", state_id, exc)

    def _gradient(self, state) -> tuple[ad.GradientGraph, np.ndarray, dict]:
        budget = state.new_budget()
        gg = state.gradient_graph(budget)
        grad_budget = state.new_budget()
        grad = ad.gradient(gg, state.point, state.layout, grad_budget)
        diag = {
            "value": gg.primal_value,
            "gradient_nodes": len(gg.expr.order),
            "roots": len(gg.roots),
            "kinks": gg.kinks,
            "budget_evals": grad_budget.evals_used,
        }
        return gg, grad, diag

    def gradient(self, state_id: str) -> dict[str, Any]:
        run_id = self.logger.new_run_id()
        state = self.store.get(state_id)
        try:
            gg, grad, diag = self._gradient(state)
            self.logger.record(
                run_id, "gradient", state_id=state_id, status="ok",
                intermediates=diag,
                result_summary={"grad_norm": float(np.linalg.norm(grad))})
            return {"value": gg.primal_value,
                    "gradient": state.layout.unpack(grad),
                    "version": state.version, "run_id": run_id}
        except HVPError as exc:
            self._fail(run_id, "gradient", state_id, exc)

    def value(self, state_id: str) -> dict[str, Any]:
        run_id = self.logger.new_run_id()
        state = self.store.get(state_id)
        try:
            x = state.require_point()
            budget = state.new_budget()
            val = state.graph.evaluate(
                [state.output], x, state.layout,
                budget=budget, stage="forward")[0]
            self.logger.record(
                run_id, "value", state_id=state_id, status="ok",
                intermediates={"budget_evals": budget.evals_used},
                result_summary={"value": float(val)})
            return {"value": float(val), "version": state.version,
                    "run_id": run_id}
        except HVPError as exc:
            self._fail(run_id, "value", state_id, exc)

    def hvp(self, state_id: str, vector: dict[str, Any]) -> dict[str, Any]:
        run_id = self.logger.new_run_id()
        state = self.store.get(state_id)
        try:
            v = state.layout.bind_vector(vector, what="vector")
            gg = state.gradient_graph(state.new_budget())
            hb = state.new_budget()
            hv = ad.hvp(gg, state.point, v, state.layout, hb, state.nonsmooth)
            is_zero = bool(np.count_nonzero(v) == 0)
            self.logger.record(
                run_id, "hvp", state_id=state_id, status="ok",
                request_summary={"zero_direction": is_zero},
                intermediates={"value": gg.primal_value,
                               "tangent_nodes_visited": hb.evals_used,
                               "v_norm": float(np.linalg.norm(v)),
                               "kinks": gg.kinks},
                result_summary={
                    "hvp_norm": float(np.linalg.norm(hv)),
                    "exact_zero": is_zero and float(np.abs(hv).max(initial=0.0)) == 0.0})
            return {"value": gg.primal_value,
                    "hvp": state.layout.unpack(hv),
                    "zero_direction": is_zero,
                    "version": state.version, "run_id": run_id}
        except HVPError as exc:
            self._fail(run_id, "hvp", state_id, exc)

    # ------------------------------------------------------------------
    def verify(self, state_id: str, vector: dict[str, Any] | None,
               tolerance: float) -> dict[str, Any]:
        """Independent numerical verification (see validation.py).

        Checks, each with an explicit verdict and failure reason/category:

        1. gradient vs mpmath 50-digit reference gradient;
        2. gradient vs independent central finite differences of primal;
        3. HVP vs explicit dense H @ v from the mpmath Hessian;
        4. zero direction returns exact zero;
        5. parameter-sharing sanity: two random directions match H @ v from
           the same explicit reference Hessian;
        6. Hessian symmetry probed via two basis directions (small n only).

        At a non-smooth point the Hessian reference is undefined, so
        derivative checks are skipped with that reason recorded.
        """
        run_id = self.logger.new_run_id()
        state = self.store.get(state_id)
        checks: list[dict[str, Any]] = []
        try:
            x = state.require_point()
            gg = state.gradient_graph(state.new_budget())
            grad = ad.gradient(gg, x, state.layout, state.new_budget())

            nonsmooth_here = bool(gg.kinks)
            ref_grad = None
            H = None
            if not nonsmooth_here:
                ref = MpmathReference(state.spec, state.layout)
                _, ref_grad, H = ref.value_gradient_hessian(x)
                checks.append(check_gradient_against_ref(
                    ref_grad, grad, tol=tolerance).to_dict())

                def primal_only(xx: np.ndarray) -> float:
                    return float(state.graph.evaluate(
                        [state.output], xx, state.layout,
                        budget=state.new_budget(), stage="forward")[0])

                fd_grad = finite_difference_gradient(primal_only, x)
                checks.append(check_gradient_against_ref(
                    fd_grad, grad, tol=1e-6,
                    name="gradient_vs_central_differences").to_dict())
            else:
                checks.append({
                    "name": "smooth_only_checks",
                    "passed": True,
                    "skipped": True,
                    "reason": ("evaluation point is a declared kink "
                               f"({gg.kinks}); high-precision gradient/Hessian "
                               "is undefined there, only the exact-zero "
                               "direction is checked"),
                    "max_abs_error": 0.0, "max_rel_error": 0.0,
                    "tolerance": tolerance,
                    "expected": [], "actual": []})

            probes: list[tuple[str, np.ndarray]] = []
            if vector is not None:
                probes.append(("user_vector",
                               state.layout.bind_vector(vector, what="vector")))
            probes.append(("zero", np.zeros_like(x)))
            if not nonsmooth_here:
                rng = np.random.default_rng(0x4A50)  # deterministic, replayable
                probes.append(("random_seed_0x4A50",
                               rng.standard_normal(x.size)))
                probes.append(("random2_seed_0x4A50",
                               rng.standard_normal(x.size)))

            for name, vv in probes:
                hv = ad.hvp(gg, x, vv, state.layout, state.new_budget(),
                            state.nonsmooth)
                if name == "zero":
                    res = check_vector_against_ref(
                        np.zeros_like(x), hv, tol=1e-12,
                        name="hvp_zero_direction")
                else:
                    res = check_vector_against_ref(
                        H @ vv, hv, tol=tolerance,
                        name=f"hvp_vs_hessian[{name}]")
                d = res.to_dict()
                d["probe"] = name
                checks.append(d)

            # symmetry probe: <e_i, H e_j> == <e_j, H e_i>
            n = x.size
            if not nonsmooth_here and n <= 16:
                i, j = 0, min(1, n - 1)
                ei = np.zeros(n)
                ej = np.zeros(n)
                ei[i] = 1.0
                ej[j] = 1.0
                hi = ad.hvp(gg, x, ei, state.layout, state.new_budget(),
                            state.nonsmooth)
                hj = ad.hvp(gg, x, ej, state.layout, state.new_budget(),
                            state.nonsmooth)
                gap = float(abs(float(hi[j]) - float(hj[i])))
                checks.append({
                    "name": "hessian_symmetry_basis_probe",
                    "passed": gap <= max(tolerance, 1e-10),
                    "reason": f"|H_ij - H_ji| = {gap:.3e}",
                    "max_abs_error": gap,
                    "max_rel_error": gap,
                    "tolerance": max(tolerance, 1e-10),
                    "expected": [float(hj[i])],
                    "actual": [float(hi[j])],
                    "probe": f"e{i},e{j}",
                })

            passed = all(c["passed"] for c in checks
                         if not c.get("skipped"))
            self.logger.record(
                run_id, "verify", state_id=state_id,
                status="ok" if passed else "check_failed",
                intermediates={"point": state.point_preview,
                               "gradient": grad.tolist(),
                               "ref_hessian": H.tolist() if H is not None else None,
                               "nonsmooth_point": nonsmooth_here},
                checks=checks,
                result_summary={
                    "passed": passed,
                    "n_checks": len(checks),
                    "n_failed": sum(1 for c in checks if not c["passed"])})
            return {"passed": passed, "checks": checks,
                    "ref_hessian": H.tolist() if H is not None else None,
                    "nonsmooth_point": nonsmooth_here,
                    "version": state.version, "run_id": run_id}
        except HVPError as exc:
            self._fail(run_id, "verify", state_id, exc, checks=checks)
