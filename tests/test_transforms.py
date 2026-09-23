import pytest

from maskcompiler.errors import RuleParameterError, UnknownTransformError
from maskcompiler.keys import generate_bundle
from maskcompiler.transforms import (
    EncryptTransform,
    HashTransform,
    MaskTransform,
    RedactTransform,
    create_transform,
)


# ---------- mask ----------

def test_mask_keeps_last_by_unicode_code_point():
    t = MaskTransform("r", {"keep_last": 4})
    assert t.apply_scalar("13812345678") == "*******5678"


def test_mask_keeps_first_and_last():
    t = MaskTransform("r", {"keep_first": 3, "keep_last": 2})
    assert t.apply_scalar("13812345678") == "138******78"


def test_mask_unicode_chars_count_as_one():
    t = MaskTransform("r", {"keep_last": 1})
    assert t.apply_scalar("张三") == "*三"
    t2 = MaskTransform("r", {"keep_first": 1, "keep_last": 1})
    assert t2.apply_scalar("孙悟空") == "孙*空"


def test_mask_prefix_and_suffix_overlap_hides_all():
    t = MaskTransform("r", {"keep_first": 2, "keep_last": 2})
    assert t.apply_scalar("abcd") == "****"
    assert t.apply_scalar("abc") == "***"


def test_mask_custom_char_and_length():
    t = MaskTransform("r", {"keep_last": 2, "mask_char": "#", "mask_length": 3})
    assert t.apply_scalar("secret") == "###et"


def test_mask_rejects_bad_params():
    with pytest.raises(RuleParameterError):
        MaskTransform("r", {"keep_last": -1})
    with pytest.raises(RuleParameterError):
        MaskTransform("r", {"keep_last": True})
    with pytest.raises(RuleParameterError):
        MaskTransform("r", {"mask_char": "**"})
    with pytest.raises(RuleParameterError):
        MaskTransform("r", {"bogus": 1})


# ---------- redact ----------

def test_redact_default_null_and_custom_replacement():
    assert RedactTransform("r", {}).apply_node({"s": "v"}) is None
    t = RedactTransform("r", {"replacement": "REDACTED"})
    assert t.apply_node({"s": "v"}) == "REDACTED"
    assert t.handles_containers is True


def test_redact_unknown_param_rejected():
    with pytest.raises(RuleParameterError):
        RedactTransform("r", {"mode": "x"})


# ---------- hash (HMAC) ----------

def test_hash_is_deterministic_and_keyed(bundle):
    t = HashTransform("r", {})
    t.bind_keys(bundle)
    a = t.apply_scalar("13812345678")
    b = t.apply_scalar("13812345678")
    assert a == b and len(a) == 64  # sha256 hex
    other = HashTransform("r", {})
    other.bind_keys(generate_bundle())
    assert other.apply_scalar("13812345678") != a  # different key -> different tag


def test_hash_algorithms_encoding_and_prefix():
    bundle = generate_bundle()
    t512 = HashTransform("r", {"algo": "sha512", "encoding": "base64", "prefix": "hmac:"})
    t512.bind_keys(bundle)
    out = t512.apply_scalar("张三")
    assert out.startswith("hmac:")
    # 512 bits -> 64 bytes -> 88 base64 chars, plus prefix
    assert len(out) == len("hmac:") + 88


def test_hash_without_key_is_an_error_not_a_default_secret():
    t = HashTransform("r", {})
    with pytest.raises(RuleParameterError):
        t.apply_scalar("x")


def test_hash_bad_algo():
    with pytest.raises(RuleParameterError):
        HashTransform("r", {"algo": "md5"})


# ---------- encrypt (Fernet) ----------

def test_encrypt_roundtrip(bundle):
    t = EncryptTransform("r", {})
    t.bind_keys(bundle)
    token = t.apply_scalar("明文-secret-123")
    assert token != "明文-secret-123"
    assert bundle.fernet().decrypt(token.encode("ascii")).decode("utf-8") == "明文-secret-123"


def test_encrypt_is_nondeterministic(bundle):
    t = EncryptTransform("r", {})
    t.bind_keys(bundle)
    assert t.apply_scalar("same") != t.apply_scalar("same")  # random IV + timestamp


# ---------- registry ----------

def test_unknown_transform_rejected():
    with pytest.raises(UnknownTransformError):
        create_transform("r", "base64-homebrew", {})
