"""泛化器单测：确定性、类型校验、失败 fail closed。"""

import datetime

import pytest

from mde.errors import GeneralizerError
from mde.generalizers import (
    g_category,
    g_date_bucket,
    g_email_mask,
    g_hash,
    g_mask,
    g_numeric_bucket,
    g_redact,
    g_replace,
)

SALT = b"unit-test-salt" * 2


def test_mask_basic():
    assert g_mask("13800001234", {"keep_prefix": 3, "keep_suffix": 4},
                  SALT) == "138****1234"
    # 过短输入不泄露任何原文。
    assert g_mask("ab", {"keep_prefix": 3, "keep_suffix": 4}, SALT) == "**"


def test_mask_rejects_non_string_surfaces_error():
    with pytest.raises(GeneralizerError):
        g_mask(True, {}, SALT)
    with pytest.raises(GeneralizerError):
        g_mask({"x": 1}, {}, SALT)


def test_email_mask():
    assert g_email_mask("alice@example.com", {}, SALT) == "a***@example.com"
    with pytest.raises(GeneralizerError):
        g_email_mask("not-an-email", {}, SALT)


def test_date_bucket_granularities():
    v = "2026-09-24T15:05:00Z"
    assert g_date_bucket(v, {"granularity": "day"}, SALT) == "2026-09-24"
    assert g_date_bucket(v, {"granularity": "month"}, SALT) == "2026-09"
    assert g_date_bucket(v, {"granularity": "year"}, SALT) == "2026"
    assert g_date_bucket(datetime.date(2026, 9, 24),
                         {"granularity": "month"}, SALT) == "2026-09"
    assert g_date_bucket(1790000000, {"granularity": "year"}, SALT) == "2026"
    with pytest.raises(GeneralizerError):
        g_date_bucket("not-a-date", {"granularity": "year"}, SALT)
    with pytest.raises(GeneralizerError):
        g_date_bucket("2026-09-24", {"granularity": "hour"}, SALT)


def test_numeric_bucket():
    bins = {"bins": [0, 18, 30, 65]}
    assert g_numeric_bucket(10, bins, SALT) == "[0,18)"
    assert g_numeric_bucket(18, bins, SALT) == "[18,30)"
    assert g_numeric_bucket(70, bins, SALT) == ">=65"
    assert g_numeric_bucket(-1, bins, SALT) == "<0"
    with pytest.raises(GeneralizerError):
        g_numeric_bucket("x", bins, SALT)
    with pytest.raises(GeneralizerError):
        g_numeric_bucket(10, {"bins": [0]}, SALT)
    with pytest.raises(GeneralizerError):
        g_numeric_bucket(10, {"bins": [30, 18]}, SALT)  # 非递增


def test_hash_is_salted_and_deterministic_per_salt():
    h1 = g_hash("alice", {}, SALT)
    h2 = g_hash("alice", {}, SALT)
    assert h1 == h2  # 同盐稳定
    h3 = g_hash("alice", {}, b"different-salt-32-bytes-long-xx")
    assert h1 != h3  # 不同盐不可关联
    assert len(h1) == 32  # 默认 16 字节 hex


def test_hash_accepts_numbers_as_strings():
    a = g_hash(123, {}, SALT)
    b = g_hash("123", {}, SALT)
    assert a == b


def test_category_mapping_and_default():
    params = {"map": {"CN": "domestic", "US": "foreign"},
              "default": "other"}
    assert g_category("CN", params, SALT) == "domestic"
    assert g_category("DE", params, SALT) == "other"
    with pytest.raises(GeneralizerError):
        g_category("DE", {"map": {"CN": "domestic"}}, SALT)


def test_redact_and_replace():
    assert g_redact("anything", {}, SALT) is None
    assert g_replace("x", {"with": "<R>"}, SALT) == "<R>"
