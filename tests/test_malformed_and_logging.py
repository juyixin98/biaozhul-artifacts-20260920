"""畸形令牌、结构攻击与“日志不泄露完整令牌”（acceptance 重点）。"""

from __future__ import annotations

import logging

import pytest

from app.jwt_sign import b64u_json, sign_jwt

from .conftest import make_issuer, register_jwks, standard_claims


@pytest.mark.parametrize("token,expected", [
    ("", "malformed_token"),
    ("aaa", "malformed_token"),
    ("aaa.bbb", "malformed_token"),
    ("aaa.bbb.ccc.ddd", "malformed_token"),
    ("@@@.bbb.ccc", "bad_base64url"),
])
def test_malformed_shapes(make_env, token, expected):
    cfg = make_issuer()
    _, client, *_ = make_env([cfg])
    resp = client.post("/verify", json={"token": token})
    assert resp.status_code in (400, 401)
    assert resp.json()["error"] == expected


def test_header_not_json_object(make_env):
    cfg = make_issuer()
    _, client, *_ = make_env([cfg])
    token = f"{b64u_json([1, 2, 3])}.{b64u_json({})}.AAAA"
    resp = client.post("/verify", json={"token": token})
    assert resp.json()["error"] == "malformed_header"


def test_zip_header_rejected(make_env, rsa_key):
    cfg = make_issuer(algs=["RS256"])
    _, client, _, source, clock = make_env([cfg])
    register_jwks(source, cfg, {"k1": (rsa_key, "RS256")})
    token = sign_jwt(
        standard_claims(now=clock.t), rsa_key,
        alg="RS256", kid="k1", extra_headers={"zip": "DEF"},
    )
    resp = client.post("/verify", json={"token": token})
    assert resp.json()["error"] == "unsupported_compression"


def test_error_body_has_stable_shape(make_env):
    cfg = make_issuer()
    _, client, *_ = make_env([cfg])
    resp = client.post("/verify", json={"token": "not-a-jwt"})
    body = resp.json()
    assert set(body) == {"error", "reason", "context", "denied"}
    assert isinstance(body["error"], str)
    assert isinstance(body["reason"], str)
    assert isinstance(body["context"], dict)
    assert body["denied"] is True


class _ListHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


def test_logs_never_contain_full_token(make_env, rsa_key, caplog):
    cfg = make_issuer(algs=["RS256"])
    _, client, _, source, clock = make_env([cfg])
    register_jwks(source, cfg, {"k1": (rsa_key, "RS256")})

    good = sign_jwt(standard_claims(now=clock.t), rsa_key,
                    alg="RS256", kid="k1")
    with caplog.at_level(logging.DEBUG, logger="jwt_gateway"):
        # 成功一次
        client.post("/verify", json={"token": good})
        # 多种失败：过期、坏签名、畸形、未知发行方
        from tests.test_time_and_claims import _token_at  # noqa
        expired = _token_at(rsa_key, cfg, clock, exp_delta=-1)
        client.post("/verify", json={"token": expired})
        client.post("/verify", json={"token": f"{good}x"})
        client.post("/verify", json={"token": "a.b.c"})
        evil = sign_jwt(
            standard_claims(iss="https://evil", now=clock.t),
            rsa_key, alg="RS256", kid="k1",
        )
        client.post("/verify", json={"token": evil})

    rendered = "\n".join(
        r.getMessage() for r in caplog.records if r.name == "jwt_gateway"
    )
    # 完整令牌及其任意一段都不得出现在日志里。
    for fragment in (good, *good.split(".")):
        assert fragment not in rendered, (
            f"日志中泄露了令牌片段: {fragment[:16]}..."
        )
    for fragment in (evil, *evil.split(".")):
        assert fragment not in rendered
    # 短指纹（sha256:xxxx）应该出现，便于关联。
    assert "sha256:" in rendered
    assert "token_expired" in rendered


def test_accept_log_contains_only_metadata(make_env, rsa_key, caplog):
    cfg = make_issuer(algs=["RS256"])
    _, client, _, source, clock = make_env([cfg])
    register_jwks(source, cfg, {"k1": (rsa_key, "RS256")})
    good = sign_jwt(standard_claims(now=clock.t), rsa_key,
                    alg="RS256", kid="k1")
    with caplog.at_level(logging.INFO, logger="jwt_gateway"):
        client.post("/verify", json={"token": good})
    lines = [r.getMessage() for r in caplog.records
             if r.name == "jwt_gateway" and "accepted" in r.getMessage()]
    assert len(lines) == 1
    line = lines[0]
    assert "issuer_id=rsa-issuer" in line
    assert "kid=k1" in line
    assert good not in line
    assert good.split(".")[1] not in line  # 载荷段
