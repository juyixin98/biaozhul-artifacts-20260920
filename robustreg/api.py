"""JSON request/response layer.

Request schema (see README for the full contract)::

    {
      "X": [[...], ...],          # required, n x p real matrix
      "y": [...],                 # required, length n
      "method": "huber" | "ols",  # optional, default "huber"
      "fit_intercept": bool,      # optional, default true
      "delta": number > 0,        # optional, default 1.345 (huber only)
      "lam": number >= 0,         # optional, default 0.0 (slopes only)
      "max_iter": int > 0,        # optional, default 50
      "tol": number > 0,          # optional, default 1e-8
      "rcond": number > 0 | null, # optional, default max(n,p)*eps
      "allow_rank_deficient": bool  # optional, default false
    }

Success response::

    {"ok": true, "method": ..., "result": {...estimator-specific...}}

Failure response::

    {"ok": false, "error": {"code": "...", "message": "..."}}

Error codes: ``invalid_request`` (schema/range problems) and
``rank_deficient`` (design matrix rank below full).
"""

from __future__ import annotations

import warnings

from robustreg.huber import huber_irls, ols_fit
from robustreg.validation import RequestError, parse_request
from robustreg.wls import RankDeficientError


def _common_rank_info(result):
    return {
        "rank": result.rank,
        "rank_deficient": result.rank_deficient,
        "nullspace_dim": result.nullspace_dim,
        "smallest_singular_value": result.smallest_singular_value,
        "singular_value_threshold": result.singular_value_threshold,
        "singular_values": [float(v) for v in result.singular_values],
    }


def _huber_response(req):
    res = huber_irls(
        req.X, req.y,
        delta=req.delta,
        fit_intercept=req.fit_intercept,
        lam=req.lam,
        max_iter=req.max_iter,
        tol=req.tol,
        rcond=req.rcond,
        allow_rank_deficient=req.allow_rank_deficient,
    )
    d = res.to_dict()
    d["fit_intercept"] = req.fit_intercept
    d["lam"] = req.lam
    d["n"] = int(req.X.shape[0])
    d["p"] = int(req.X.shape[1])
    d["status"] = (
        "converged" if res.converged
        else ("max_iter_reached" if res.iterations >= res.max_iter
              else "not_converged")
    )
    return {"ok": True, "method": "huber", "result": d}


def _ols_response(req):
    res = ols_fit(
        req.X, req.y,
        fit_intercept=req.fit_intercept,
        rcond=req.rcond,
        allow_rank_deficient=req.allow_rank_deficient,
        delta=req.delta,
    )
    d = res.to_dict()
    d["fit_intercept"] = req.fit_intercept
    d["n"] = int(req.X.shape[0])
    d["p"] = int(req.X.shape[1])
    d["delta_used_for_huber_objective"] = req.delta
    return {"ok": True, "method": "ols", "result": d}


def handle_request(request):
    """Validate and execute one request; always returns a JSONable dict.

    Rank-deficiency warnings raised inside the estimators are recorded in the
    result's ``warnings`` list anyway; here they are additionally suppressed
    from stderr so a JSON/CLI consumer gets clean stdout only.
    """
    try:
        req = parse_request(request)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            if req.method == "huber":
                return _huber_response(req)
            return _ols_response(req)
    except RequestError as exc:
        return {
            "ok": False,
            "error": {"code": "invalid_request", "message": str(exc)},
        }
    except RankDeficientError as exc:
        return {
            "ok": False,
            "error": {
                "code": "rank_deficient",
                "message": str(exc),
                "rank": int(exc.rank),
                "p": int(exc.p),
                "nullspace_dim": int(exc.nullspace_dim),
                "smallest_singular_value": float(exc.smallest_singular_value),
                "threshold": float(exc.threshold),
                "hint": (
                    "remove redundant columns, add lam>0 ridge penalty on "
                    "slopes, or set allow_rank_deficient=true to obtain the "
                    "SVD minimum-norm solution"
                ),
            },
        }
