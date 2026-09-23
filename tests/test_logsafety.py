import io
import logging

import pytest

from maskcompiler.compiler import compile_ruleset
from maskcompiler.engine import apply_ruleset
from maskcompiler.errors import RuleConflictError, TypeMismatchError
from maskcompiler.logsafe import (
    SecretRedactionFilter,
    register_secret,
    secret_scope,
)


def _logger_with_buffer():
    logger = logging.getLogger("maskcompiler.test.redaction")
    logger.handlers.clear()
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setLevel(logging.DEBUG)
    handler.addFilter(SecretRedactionFilter())
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    return logger, stream


SECRET = "13812345678"


def test_logged_original_value_is_redacted_on_success():
    logger, stream = _logger_with_buffer()
    doc = {"phone": SECRET}
    compiled = compile_ruleset(
        {"version": 1, "rules": [
            {"id": "p", "path": "$.phone", "transform": "mask",
             "params": {"keep_last": 4}, "priority": 1}
        ]}
    )
    with secret_scope():
        apply_ruleset(compiled, doc)
        register_secret(SECRET)
        logger.warning("debug dump phone=%s extra %s", SECRET, "suffix-" + SECRET)
    text = stream.getvalue()
    assert SECRET not in text
    assert "***" in text


def test_type_mismatch_error_does_not_contain_value():
    doc = {"n": 123456789}
    compiled = compile_ruleset(
        {"version": 1, "rules": [
            {"id": "n", "path": "$.n", "transform": "mask",
             "params": {"keep_last": 1}, "priority": 1}
        ]}
    )
    with pytest.raises(TypeMismatchError) as exc:
        apply_ruleset(compiled, doc)
    assert "123456789" not in str(exc.value)


def test_conflict_traceback_in_logs_is_scrubbed():
    logger, stream = _logger_with_buffer()
    doc = {"phone": SECRET}
    compiled = compile_ruleset(
        {"version": 1, "rules": [
            {"id": "a", "path": "$[*]", "transform": "redact", "priority": 10},
            {"id": "b", "path": "$.phone", "transform": "mask",
             "params": {"keep_last": 4}, "priority": 10},
        ]}
    )
    with secret_scope():
        try:
            apply_ruleset(compiled, doc)
        except RuleConflictError:
            logger.exception("apply failed for phone %s", SECRET)
    text = stream.getvalue()
    assert SECRET not in text
    assert "13812345678" not in text


def test_redaction_filter_handles_non_string_args():
    logger, stream = _logger_with_buffer()
    with secret_scope():
        register_secret(SECRET)
        logger.info("payload=%r", {"phone": SECRET, "n": 1})
    assert SECRET not in stream.getvalue()
