import pytest

from mde.transforms import (
    TransformContext,
    TransformError,
    apply_transform,
    validate_transform,
)


@pytest.fixture()
def ctx():
    return TransformContext(b"secret-32-bytes-secret-32-bytes!", "fp", "analytics")


def test_email_domain(ctx):
    assert apply_transform("email_domain", "A@B.Example.COM", {}, ctx) == "b.example.com"
    with pytest.raises(TransformError):
        apply_transform("email_domain", "not-email", {}, ctx)
    with pytest.raises(TransformError):
        apply_transform("email_domain", 123, {}, ctx)


def test_date_trunc(ctx):
    f = lambda v, u: apply_transform("date_trunc", v, {"unit": u}, ctx)
    assert f("2026-03-17T08:00:00", "month") == "2026-03"
    assert f("2026-03-17", "year") == "2026"
    assert f("2026-03-17", "day") == "2026-03-17"
    with pytest.raises(TransformError):
        f("not-a-date", "month")


def test_bucket_number(ctx):
    f = lambda v: apply_transform("bucket_number", v, {"width": 10, "offset": 0}, ctx)
    assert f(0) == "[0,10)"
    assert f(9) == "[0,10)"
    assert f(-1) == "[-10,0)"
    with pytest.raises(TransformError):
        f(True)  # bool 不得被当作整数
    validate_transform("bucket_number", {"width": 5, "offset": -10})
    with pytest.raises(TransformError):
        validate_transform("bucket_number", {"width": 0, "offset": 0})


def test_mask_tail_fixed_width(ctx):
    f = lambda v: apply_transform("mask_tail", v,
                                  {"keep_prefix": 2, "mask": "*"}, ctx)
    a = f("13800000000")
    b = f("13")
    # 掩码长度固定（不泄露原值长度）
    assert a == "13********" and b == "13********"


def test_pseudonymize_deterministic_and_purpose_bound(ctx):
    p1 = validate_transform("pseudonymize", {"scope": "purpose"})
    other = TransformContext(b"secret-32-bytes-secret-32-bytes!", "fp", "support")
    v1 = apply_transform("pseudonymize", "Zhang", p1, ctx)
    v2 = apply_transform("pseudonymize", "Zhang", p1, ctx)
    v3 = apply_transform("pseudonymize", "Zhang", p1, other)
    assert v1 == v2 and v1 != v3
    assert v1.startswith("hmac:")


def test_policy_fingerprint_binds_pseudonym(ctx):
    diff = TransformContext(b"secret-32-bytes-secret-32-bytes!", "other-fp", "analytics")
    params = validate_transform("pseudonymize", {"scope": "purpose"})
    assert apply_transform("pseudonymize", "x", params, ctx) != \
           apply_transform("pseudonymize", "x", params, diff)


def test_unknown_transform_rejected():
    with pytest.raises(TransformError):
        validate_transform("rot13", {})
