"""发行方配置（信任根）的加载与校验测试。"""

from __future__ import annotations

import json

import pytest

from app.config import load_trust_store
from app.errors import ErrCode, VerifyError


def _write(tmp_path, obj):
    p = tmp_path / "issuers.json"
    p.write_text(json.dumps(obj), encoding="utf-8")
    return p


_VALID = {
    "issuers": [
        {
            "id": "a",
            "iss": "https://a.example",
            "jwks_uri": "https://a.example/jwks",
            "allowed_algs": ["RS256"],
            "audiences": ["aud1"],
        }
    ]
}


def test_load_valid(tmp_path):
    store = load_trust_store(_write(tmp_path, _VALID))
    cfg = store.get("https://a.example")
    assert cfg is not None and cfg.id == "a"
    assert cfg.allowed_algs == frozenset({"RS256"})
    # 默认 leeway=0, cache_ttl=300
    assert cfg.leeway == 0 and cfg.cache_ttl == 300


def test_unknown_alg_in_config_rejected(tmp_path):
    obj = json.loads(json.dumps(_VALID))
    obj["issuers"][0]["allowed_algs"] = ["RS256", "HS999"]
    with pytest.raises(VerifyError) as ei:
        load_trust_store(_write(tmp_path, obj))
    assert ei.value.code == ErrCode.JWKS_MALFORMED


def test_plain_http_non_loopback_rejected(tmp_path):
    obj = json.loads(json.dumps(_VALID))
    obj["issuers"][0]["jwks_uri"] = "http://intranet.corp/jwks"
    with pytest.raises(VerifyError) as ei:
        load_trust_store(_write(tmp_path, obj))
    assert ei.value.code == ErrCode.JWKS_FETCH_FAILED


def test_http_loopback_allowed(tmp_path):
    obj = json.loads(json.dumps(_VALID))
    obj["issuers"][0]["jwks_uri"] = "http://127.0.0.1:8787/jwks"
    store = load_trust_store(_write(tmp_path, obj))
    assert store.get("https://a.example").jwks_uri.endswith("/jwks")


def test_hmac_short_secret_rejected(tmp_path):
    obj = {"issuers": [{
        "id": "h", "iss": "https://h",
        "allowed_algs": ["HS256"],
        "hmac_secret": "too-short",
        "audiences": ["a"],
    }]}
    with pytest.raises(VerifyError) as ei:
        load_trust_store(_write(tmp_path, obj))
    assert ei.value.code == ErrCode.JWKS_MALFORMED


def test_duplicate_iss_rejected(tmp_path):
    obj = {"issuers": [
        {"id": "a", "iss": "https://x", "jwks_uri": "https://x/j",
         "allowed_algs": ["RS256"], "audiences": ["z"]},
        {"id": "b", "iss": "https://x", "jwks_uri": "https://y/j",
         "allowed_algs": ["RS256"], "audiences": ["z"]},
    ]}
    with pytest.raises(VerifyError) as ei:
        load_trust_store(_write(tmp_path, obj))
    assert ei.value.code == ErrCode.JWKS_MALFORMED


def test_asym_issuer_requires_jwks_uri(tmp_path):
    obj = {"issuers": [{
        "id": "a", "iss": "https://x",
        "allowed_algs": ["RS256"], "audiences": ["z"],
    }]}
    with pytest.raises(VerifyError):
        load_trust_store(_write(tmp_path, obj))


def test_empty_issuers_rejected(tmp_path):
    with pytest.raises(VerifyError):
        load_trust_store(_write(tmp_path, {"issuers": []}))


def test_negative_leeway_rejected(tmp_path):
    obj = json.loads(json.dumps(_VALID))
    obj["issuers"][0]["leeway"] = -1
    with pytest.raises(VerifyError):
        load_trust_store(_write(tmp_path, obj))


def test_demo_config_file_loads():
    # 仓库自带的演示配置必须可加载。
    store = load_trust_store("config/issuers.json")
    assert len(store.issuers) == 3
