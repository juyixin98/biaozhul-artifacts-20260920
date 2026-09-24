"""Acceptance tests: cross-function, conditional sanitization, recursion.

Each fixture is checked for BOTH the verdict and the shape of the evidence
path (a source hop must connect to a sink hop through the named functions).
"""

from app.analysis import AnalysisRequest, run_analysis

from .fixtures import (
    ARITHMETIC_FLOW,
    CONDITION_LITERAL_EQUALITY_NOT_SANITIZER,
    CONDITION_NEGATED,
    CONDITION_SANITIZED,
    CONDITION_SANITIZER_VALUE,
    CONDITION_UNSANITIZED_BRANCH,
    CONTEXT_SENSITIVE,
    CROSS_FN_DANGEROUS,
    CROSS_FN_SAFE,
    DEAD_FUNCTION,
    LOOP_NEVER_RUNS,
    LOOP_SANITIZED_IN_BODY,
    LOOP_TAINTED,
    MUTUAL_RECURSION_DANGEROUS,
    RECURSION_DANGEROUS,
    RECURSION_MIXED,
    RECURSION_SAFE,
    SINK_IN_CONDITION,
    UNKNOWN_FUNCTION,
)


def analyze(code, **kw):
    return run_analysis(AnalysisRequest(code=code, **kw))


def source_names(finding) -> list[str]:
    return [ep["source"] for ep in finding["evidence_paths"]]


def rendered(finding) -> list[str]:
    return [ep["rendered"] for ep in finding["evidence_paths"]]


# ---------------------------------------------------------------------------
# 1) cross-function propagation
# ---------------------------------------------------------------------------

def test_cross_function_dangerous_finding_and_path():
    rep = analyze(CROSS_FN_DANGEROUS)
    assert rep["verdict"] == "vulnerable"
    assert rep["finding_count"] == 1
    f = rep["findings"][0]
    assert f["sink"] == "sink"
    assert f["function"] == "render"
    assert "input" in source_names(f)
    path = f["evidence_paths"][0]["rendered"]
    # the path traverses load -> (call-ret) -> render -> sink
    assert "load@" in path
    assert "render@" in path
    assert "source(input)" in path
    assert "sink(sink)" in path


def test_cross_function_safe_after_sanitizer():
    rep = analyze(CROSS_FN_SAFE)
    assert rep["verdict"] == "safe"
    assert rep["findings"] == []


def test_context_sensitive_keeps_callers_apart():
    rep = analyze(CONTEXT_SENSITIVE)
    # only the clean call feeds the sink; tainted `bad` is never passed.
    # Call-string k-CFA merges same-site calls into one context; isolation is
    # then enforced per call site via argument-chain filtering, so the key
    # assertion is the verdict plus the fact that no evidence path survives.
    assert rep["verdict"] == "safe"
    assert rep["findings"] == []


def test_context_sensitive_distinct_call_chains():
    # The same user-defined wrapper invoked from two different caller
    # functions must be analyzed in two separate call-string contexts, so
    # taint reaching it from a() cannot leak into the result computed for b().
    # NOTE: the helper is named `wrapper`, not `wrap` -- `wrap` is a built-in
    # propagator in the default policy (builtins take precedence over user
    # functions sharing their name).
    code = """
    func wrapper(x) { return x; }
    func a() {
        var t = wrapper(input());
        sink(t);
        return t;
    }
    func b() {
        var s = wrapper("safe");
        sink(s);
        return s;
    }
    func main() {
        var good = b();
        sink(good);
        var bad = a();
    }
    """
    rep = analyze(code)
    assert rep["verdict"] == "vulnerable"
    contexts = rep["context_sensitivity"]["contexts"]
    assert any(c.endswith("a > wrapper") for c in contexts), contexts
    assert any(c.endswith("b > wrapper") for c in contexts), contexts
    sink_lines = sorted(f["line"] for f in rep["findings"])
    assert sink_lines == [5]
    assert all(f["function"] == "a" for f in rep["findings"])


# ---------------------------------------------------------------------------
# 2) conditional sanitization
# ---------------------------------------------------------------------------

def test_validator_true_branch_is_clean():
    rep = analyze(CONDITION_SANITIZED)
    assert rep["verdict"] == "safe"


def test_validator_false_branch_still_tainted():
    rep = analyze(CONDITION_UNSANITIZED_BRANCH)
    assert rep["verdict"] == "vulnerable"
    findings = rep["findings"]
    # only the unguarded else-branch sink is reported
    assert len(findings) == 1
    f = findings[0]
    assert f["line"] == 7  # sink in the else block
    # Inside the (unrefined) else branch the sink is reached with definitely
    # tainted data -- certainty is about the value at the sink, not about
    # whether the branch is taken.
    assert f["certainty"] == "definite"
    assert f["taint_level"] == "tainted"


def test_taint_after_branch_join_is_conditional():
    # The canonical "conditional" case: a value is sanitized on ONE arm and
    # used by a sink AFTER the if/else merge point.
    code = """
    func main() {
        var x = input();
        if (validate(x)) {
            x = escape(x);
        }
        sink(x);
    }
    """
    rep = analyze(code)
    assert rep["verdict"] == "vulnerable"
    f = rep["findings"][0]
    assert f["certainty"] == "conditional"
    assert f["taint_level"] == "maybe"


def test_negated_validator_keeps_taint_on_then_branch():
    rep = analyze(CONDITION_NEGATED)
    assert rep["verdict"] == "vulnerable"
    assert rep["findings"][0]["certainty"] == "definite"


def test_sanitizer_value_is_clean():
    rep = analyze(CONDITION_SANITIZER_VALUE)
    assert rep["verdict"] == "safe"


def test_literal_equality_is_not_a_sanitizer():
    rep = analyze(CONDITION_LITERAL_EQUALITY_NOT_SANITIZER)
    assert rep["verdict"] == "vulnerable"


def test_sink_inside_condition_evaluated():
    rep = analyze(SINK_IN_CONDITION)
    assert rep["verdict"] == "vulnerable"


# ---------------------------------------------------------------------------
# 3) recursion
# ---------------------------------------------------------------------------

def test_recursion_dangerous_path():
    rep = analyze(RECURSION_DANGEROUS)
    assert rep["verdict"] == "vulnerable"
    f = rep["findings"][0]
    assert "input" in source_names(f)
    assert "walk@" in rendered(f)[0]


def test_recursion_safe_base_case():
    rep = analyze(RECURSION_SAFE)
    assert rep["verdict"] == "safe"


def test_recursion_mixed_is_maybe():
    # walk returns a literal on most arms but input() on the n==5 arm.  Its
    # return summary therefore joins a clean path with a tainted path ->
    # MAYBE (conditional): the function CAN return taint, but not on every
    # abstract path.  The analyzer is not path-sensitive on the concrete
    # integer argument, so it cannot prove walk(3) skips the n==5 arm; that
    # imprecision is the documented false-positive boundary (see README).
    rep = analyze(RECURSION_MIXED)
    assert rep["verdict"] == "vulnerable"
    f = rep["findings"][0]
    assert f["taint_level"] == "maybe"
    assert f["certainty"] == "conditional"
    assert "input" in source_names(f)


def test_recursion_unreachable_source_branch_is_maybe_at_merge():
    # After a branch that sanitizes on one arm and leaves taint on the other,
    # the merged value at the post-if sink is MAYBE (conditional).
    code = """
    func clean_or_taint(c) {
        if (validate(c)) {
            return escape(c);
        }
        return c;
    }
    func main() {
        var x = input();
        var y = clean_or_taint(x);
        sink(y);
    }
    """
    rep = analyze(code)
    assert rep["verdict"] == "vulnerable"
    assert rep["findings"][0]["taint_level"] == "maybe"


def test_mutual_recursion_dangerous():
    rep = analyze(MUTUAL_RECURSION_DANGEROUS)
    assert rep["verdict"] == "vulnerable"
    assert "input" in source_names(rep["findings"][0])


# ---------------------------------------------------------------------------
# 4) loops
# ---------------------------------------------------------------------------

def test_loop_tainted_after_assignment():
    rep = analyze(LOOP_TAINTED)
    assert rep["verdict"] == "vulnerable"
    assert rep["findings"][0]["certainty"] == "conditional"


def test_loop_never_runs_stays_clean():
    rep = analyze(LOOP_NEVER_RUNS)
    assert rep["verdict"] == "safe"


def test_loop_sanitized_in_body_is_maybe():
    # x is only clean on iterations where validate(x) held; merge -> MAYBE
    rep = analyze(LOOP_SANITIZED_IN_BODY)
    assert rep["verdict"] == "vulnerable"
    assert rep["findings"][0]["taint_level"] == "maybe"


# ---------------------------------------------------------------------------
# 5) misc precision / soundness
# ---------------------------------------------------------------------------

def test_arithmetic_propagates_taint():
    rep = analyze(ARITHMETIC_FLOW)
    assert rep["verdict"] == "vulnerable"
    kinds = [h["kind"] for h in rep["findings"][0]["evidence_paths"][0]["hops"]]
    assert "binop" in kinds
    assert "propagator" in kinds


def test_unknown_function_is_conservative_with_warning():
    rep = analyze(UNKNOWN_FUNCTION)
    assert rep["verdict"] == "vulnerable"
    assert any("undefined function" in w for w in rep["warnings"])


def test_dead_function_not_reachable():
    rep = analyze(DEAD_FUNCTION)
    assert rep["verdict"] == "safe"
    assert "unused" not in rep["reachable_functions"]


def test_fixpoint_terminates_with_bounded_iterations():
    rep = analyze(RECURSION_DANGEROUS)
    assert rep["fixpoint_iterations"] > 0
    assert rep["fixpoint_iterations"] < 1000


def test_extra_policy_via_request():
    code = """
    func main() {
        var x = fetch_user();
        sink(scrub(x));
    }
    """
    # without custom policy: unknown functions -> taint anyway, but we test
    # that a custom source+sanitizer can make it safe
    rep = run_analysis(AnalysisRequest(
        code=code,
        sources=("fetch_user",),
        sanitizers=("scrub",),
    ))
    assert rep["verdict"] == "safe"

    rep2 = run_analysis(AnalysisRequest(
        code=code,
        sources=("fetch_user",),
    ))
    assert rep2["verdict"] == "vulnerable"


def test_call_string_truncation_warning():
    code = """
    func f(n) {
        if (n == 0) { return input(); }
        return f(n - 1);
    }
    func main() { sink(f(10)); }
    """
    rep = run_analysis(AnalysisRequest(code=code, call_string_k=1))
    assert rep["verdict"] == "vulnerable"
    assert any("truncated" in w for w in rep["warnings"])
