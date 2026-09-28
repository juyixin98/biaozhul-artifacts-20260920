"""Direct kernel-level tests, bypassing HTTP.

Covers parser->NFA->DFA determinisation internals and lexing with a
hand-written acceptance oracle (Python ``re``), so the automata machinery is
exercised even where no endpoint is involved.
"""

from __future__ import annotations

import re

import pytest

from app.config import Settings
from app.corpus.spec import RuleSetSpec
from app.errors import AppError, ErrorCategory
from app.kernel.compiler import compile_ruleset
from app.kernel.lexer import lex
from app.runlog import RunLogger

SETTINGS = Settings(db_path=":memory:")


class _Collector:
    def __init__(self):
        self.entries = []

    def log(self, stage, **detail):
        self.entries.append((stage, detail))


def _compile(rules):
    spec = RuleSetSpec.model_validate({"name": "direct", "rules": rules})
    collector = _Collector()
    return compile_ruleset(spec, SETTINGS, collector), collector


def _expect_error(rules, category):
    spec = RuleSetSpec.model_validate({"name": "direct-bad", "rules": rules})
    with pytest.raises(AppError) as exc:
        compile_ruleset(spec, SETTINGS, _Collector())
    assert exc.value.category is category
    return exc.value


def test_dfa_language_matches_python_re_for_samples():
    rules = [
        {"name": "R", "pattern": "(ab|a)(c*)+[0-9]{2}"},
    ]
    compiled, _ = _compile(rules)
    samples = ["ab12", "acc33", "a00", "abcccccc77"]
    for sample in samples:
        tokens, error = lex(compiled, sample)
        assert error is None, sample
        assert tokens[0].text == sample
        assert re.fullmatch(r"(ab|a)(c*)+[0-9]{2}", sample)
    for sample in ["ab1", "b12", "ab123", ""]:
        tokens, error = lex(compiled, sample)
        if sample == "":
            assert tokens == [] and error is None
        else:
            assert error is not None, sample


def test_nfa_state_limit_is_enforced():
    tiny = Settings(db_path=":memory:", max_repeat=1000, max_nfa_states=10)
    spec = RuleSetSpec.model_validate(
        {"name": "blowup", "rules": [{"name": "R", "pattern": "a{50}"}]}
    )
    with pytest.raises(AppError) as exc:
        compile_ruleset(spec, tiny, _Collector())
    assert exc.value.category is ErrorCategory.RESOURCE_EXHAUSTED


def test_nullable_patterns_rejected_directly():
    for pattern in ["", "x*", "(a|b)?", "p{0}"]:
        if not pattern:
            continue  # pydantic rejects empty pattern before the kernel
        err = _expect_error(
            [{"name": "R", "pattern": pattern}], ErrorCategory.INPUT_ERROR
        )
        assert "empty string" in err.message


def test_diagnostics_stages_logged():
    _, collector = _compile(
        [
            {"name": "A", "pattern": "abc"},
            {"name": "B", "pattern": "ab.?"},
        ]
    )
    stages = [stage for stage, _ in collector.entries]
    assert stages == ["parse", "parse", "nfa", "dfa", "diagnostics"]
    nfa_states = next(detail for stage, detail in collector.entries if stage == "nfa")
    dfa_states = next(detail for stage, detail in collector.entries if stage == "dfa")
    assert nfa_states["states"] > 0
    assert dfa_states["states"] > 0
