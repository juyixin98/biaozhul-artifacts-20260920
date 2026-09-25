"""Test helpers shared across the suite."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from resflow.engine import analyze
from resflow.parser import parse_source
from resflow.analyzer import analyze_function, Options
from resflow.errors import ResFlowError


def analyze_text(source, **kw):
    return analyze(source, **kw)


def first_function(source, **kw):
    fns = parse_source(source)
    return analyze_function(fns[0], Options(**kw))


def codes(result):
    """Multiset of diagnostic codes from a FunctionAnalysis.to_dict()."""
    return [d["code"] for d in result["diagnostics"]]


def codes_by_path(fn_dict):
    """Map path id -> sorted diagnostic code list."""
    out = {}
    for p in fn_dict["paths"]:
        out[p["id"]] = sorted(d["code"] for d in p["diagnostics"])
    return out


def expect_error(source):
    try:
        analyze(source)
    except ResFlowError:
        return
    raise AssertionError("expected ResFlowError")
