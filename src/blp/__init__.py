"""blp —— 有界线性规划纯后端求解包（两阶段单纯形）。

快速入口::

    from blp import make_lp, solve_lp
    from blp.jsonio import run_request
"""

from .errors import LPError, LPInputError, LPLimitError, LPNumericalError
from .model import (
    LP, MAX_ABS_COEFF, MAX_COLUMNS, MAX_CONSTRAINTS, MAX_ITERATIONS,
    MAX_VARIABLES, TOL, Tolerances, StandardForm, build_standard_form,
    make_lp,
)
from .solver import SolveResult, solve_lp
from .enumerate import EnumerationLimit, best_vertex_value, enumerate_vertices
from .verify import ray_residuals, solution_residuals, verify_certificate
from .simplex import RULES, run_phase
from .jsonio import run_request

__all__ = [
    "LP", "StandardForm", "SolveResult",
    "make_lp", "build_standard_form", "solve_lp",
    "run_phase", "RULES", "enumerate_vertices",
    "best_vertex_value", "EnumerationLimit",
    "solution_residuals", "ray_residuals", "verify_certificate",
    "run_request",
    "LPError", "LPInputError", "LPNumericalError", "LPLimitError",
    "TOL", "Tolerances",
    "MAX_VARIABLES", "MAX_CONSTRAINTS", "MAX_COLUMNS",
    "MAX_ITERATIONS", "MAX_ABS_COEFF",
]
