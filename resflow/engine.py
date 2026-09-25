"""High-level facade: source -> parse -> analyze -> JSON-ready report."""

from .parser import parse_source
from .analyzer import analyze_program, Options

LANGUAGE_NAME = "ResFlow"
LANGUAGE_VERSION = "1.0.0"
TOOLCHAIN_VERSION = "1.0.0"


def analyze(source, loop_bound=2, max_paths=512):
    """Analyze one ResFlow source string.

    Returns a JSON-serializable dict (shape documented in README).
    Lexical/parse failures propagate as
    :class:`~resflow.errors.ResFlowError`.
    """
    functions = parse_source(source)
    options = Options(loop_bound=loop_bound, max_paths=max_paths)
    results = analyze_program(functions, options)
    function_dicts = [r.to_dict() for r in results]
    return {
        "language": LANGUAGE_NAME,
        "language_version": LANGUAGE_VERSION,
        "toolchain_version": TOOLCHAIN_VERSION,
        "options": {"loop_bound": loop_bound, "max_paths": max_paths},
        "functions": function_dicts,
        "summary": {
            "function_count": len(function_dicts),
            "path_count": sum(f["summary"]["path_count"]
                              for f in function_dicts),
            "diagnostic_count": sum(
                f["summary"]["diagnostic_count"] for f in function_dicts),
        },
    }
