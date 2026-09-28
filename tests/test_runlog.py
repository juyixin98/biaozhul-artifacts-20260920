"""运行日志与错误类别契约：运行编号、中间状态、判断理由、类别可区分。"""

from app.errors import (
    AppError,
    ComputationFailed,
    ErrorCategory,
    InputError,
    ResourceExhausted,
    StateConflict,
)
from app.kernel.lexer import CompiledLexer
from app.main import _STATUS_BY_CATEGORY
from app.runlog import RunLogger, new_run_id

from conftest import make_rules


def test_run_id_format_unique():
    ids = {new_run_id() for _ in range(50)}
    assert len(ids) == 50
    assert all(r.startswith("run-") for r in ids)


def test_build_logs_intermediate_states(limits):
    logger = RunLogger(new_run_id())
    CompiledLexer.build(make_rules([("A", "a+"), ("B", "b")]), limits, logger)
    events = [e.event for e in logger.entries]
    assert events == ["rule_compiled", "rule_compiled", "dfa_built"]
    dfa = logger.entries[-1]
    assert dfa.state["nfa_states"] > 0
    assert dfa.state["dfa_states"] > 0
    assert dfa.rationale
    assert all(e.run_id == logger.run_id for e in logger.entries)
    assert [e.seq for e in logger.entries] == [0, 1, 2]


def test_lex_run_logged(limits):
    logger = RunLogger(new_run_id())
    lexer = CompiledLexer.build(make_rules([("A", "a+")]), limits, logger)
    lexer.tokenize("aaa", limits, logger)
    done = [e for e in logger.entries if e.event == "lex_done"]
    assert done and done[0].state == {"input_chars": 3, "tokens": 1}


def test_error_categories_distinguishable():
    assert InputError("c", "m").category is ErrorCategory.INPUT_ERROR
    assert StateConflict("c", "m").category is ErrorCategory.STATE_CONFLICT
    assert ResourceExhausted("c", "m").category is ErrorCategory.RESOURCE_EXHAUSTED
    assert ComputationFailed("c", "m").category is ErrorCategory.COMPUTATION_FAILED
    # HTTP 映射覆盖全部四类
    assert set(_STATUS_BY_CATEGORY) == set(ErrorCategory)
    assert _STATUS_BY_CATEGORY[ErrorCategory.INPUT_ERROR] == 400
    assert _STATUS_BY_CATEGORY[ErrorCategory.STATE_CONFLICT] == 409
    assert _STATUS_BY_CATEGORY[ErrorCategory.RESOURCE_EXHAUSTED] == 413
    assert _STATUS_BY_CATEGORY[ErrorCategory.COMPUTATION_FAILED] == 500


def test_error_payload_shape():
    payload = AppError("CODE", "msg", {"k": 1}).to_payload()
    assert payload == {
        "error": {
            "category": "computation_failed",
            "code": "CODE",
            "message": "msg",
            "details": {"k": 1},
        }
    }
