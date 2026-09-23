"""Quoting pipeline: normalise -> solve D -> solve y -> cross-check -> round.

A quote is executable only if *all* of the following hold:
  1. the Newton solve for D converged within the iteration budget;
  2. the Newton solve for y converged within the iteration budget;
  3. the independent bisection reference converged and agrees with Newton
     within REFERENCE_TOLERANCE normalised units.

Otherwise `executable` is False and `quote` is null — a non-converged or
disputed result never yields a tradable quote.
"""

from __future__ import annotations

from .diagnostics import invariant_residual_float
from .pool import (
    PoolStateError,
    compute_d,
    denormalize,
    get_y,
    get_y_bisection,
    invariant_residual_exact,
    normalize,
)
from .schemas import Quote, QuoteRequest, QuoteResponse, SolverReport

# Newton and bisection each converge to within CONVERGENCE_TOL of the true
# root, so their integer results may legitimately differ by a few units.
REFERENCE_TOLERANCE = 4


def _report(method: str, r) -> SolverReport:
    return SolverReport(method=method, converged=r.converged,
                        iterations=r.iterations, residual=str(r.residual),
                        converged_via=r.converged_via)


def execute_quote(req: QuoteRequest) -> QuoteResponse:
    i_in = req.token_in
    i_out = 1 - i_in
    dec_in = req.decimals[i_in]
    dec_out = req.decimals[i_out]

    x_in = normalize(int(req.reserves[i_in]), dec_in)
    x_out = normalize(int(req.reserves[i_out]), dec_out)
    dx = normalize(int(req.amount_in), dec_in)

    try:
        d_result = compute_d(x_in, x_out, req.amplification, req.max_iter)
    except PoolStateError as exc:
        return QuoteResponse(status="invalid_pool", executable=False, reason=str(exc))

    d_report = _report("newton-d", d_result)
    if not d_result.converged:
        return QuoteResponse(
            status="not_converged", executable=False,
            reason=f"invariant solve for D did not converge within {req.max_iter} iterations",
            d_before=None, newton=d_report,
        )
    d = d_result.value

    x_in_after = x_in + dx
    try:
        y_result = get_y(x_in_after, d, req.amplification, req.max_iter)
        ref_result = get_y_bisection(x_in_after, d, req.amplification, req.max_iter)
    except PoolStateError as exc:
        return QuoteResponse(status="invalid_pool", executable=False, reason=str(exc),
                             d_before=str(d), newton=d_report)

    newton_report = SolverReport(
        method="newton-y",
        converged=d_result.converged and y_result.converged,
        iterations=d_result.iterations + y_result.iterations,
        residual=str(max(d_result.residual, y_result.residual)),
        converged_via=y_result.converged_via,
    )
    ref_report = _report("bisection-y", ref_result)

    if not y_result.converged:
        return QuoteResponse(
            status="not_converged", executable=False,
            reason=f"output solve for y did not converge within {req.max_iter} iterations",
            d_before=str(d), newton=newton_report, bisection_reference=ref_report,
        )
    if not ref_result.converged:
        return QuoteResponse(
            status="not_converged", executable=False,
            reason=f"bisection reference did not converge within {req.max_iter} iterations",
            d_before=str(d), newton=newton_report, bisection_reference=ref_report,
        )

    y_newton = y_result.value
    y_ref = ref_result.value
    if abs(y_newton - y_ref) > REFERENCE_TOLERANCE:
        return QuoteResponse(
            status="solver_disagreement", executable=False,
            reason=(f"Newton result {y_newton} and bisection reference {y_ref} "
                    f"differ by more than {REFERENCE_TOLERANCE} normalised units"),
            d_before=str(d), newton=newton_report, bisection_reference=ref_report,
        )

    dy_norm = x_out - y_newton
    if dy_norm < 0:
        return QuoteResponse(
            status="solver_disagreement", executable=False,
            reason="solved output balance exceeds current balance (negative output)",
            d_before=str(d), newton=newton_report, bisection_reference=ref_report,
        )
    dy_ref_norm = x_out - y_ref

    # Post-trade invariant diagnostics (reported, not enforced as a gate).
    # Skipped when the trade drains the output side to zero, where the
    # invariant has no finite solution.
    d_after = None
    res_exact = None
    res_float = None
    if y_newton > 0:
        try:
            d1 = compute_d(x_in_after, y_newton, req.amplification, req.max_iter)
            if d1.converged:
                d_after = str(d1.value)
            res_exact = str(invariant_residual_exact(x_in_after, y_newton, d, req.amplification))
            res_float = invariant_residual_float(x_in_after, y_newton, d, req.amplification)
        except PoolStateError:
            pass

    amount_out = denormalize(dy_norm, dec_out)
    return QuoteResponse(
        status="ok",
        executable=True,
        quote=Quote(token_out=i_out,
                    amount_out=str(amount_out),
                    amount_out_normalized=str(dy_norm)),
        d_before=str(d),
        d_after=d_after,
        invariant_residual_exact=res_exact,
        invariant_residual_float=res_float,
        newton=newton_report,
        bisection_reference=ref_report,
        reference_amount_out_normalized=str(dy_ref_norm),
    )
