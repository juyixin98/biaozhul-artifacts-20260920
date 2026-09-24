"""端到端测试：通过 HTTP 接口验证各类证书链场景。"""

from datetime import datetime, timedelta, timezone


def _payload(pki, leaf_pem, dns="www.example.com", intermediates=None, roots=None, **extra):
    body = {
        "leaf_certificate_pem": leaf_pem,
        "intermediate_certificates_pem": (
            intermediates if intermediates is not None else [pki.inter_pem]
        ),
        "trust_roots_pem": roots if roots is not None else [pki.root_pem],
        "expected_dns_name": dns,
    }
    body.update(extra)
    return body


def test_health(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_valid_chain_accepted(client, pki):
    resp = client.post("/api/v1/verify", json=_payload(pki, pki.leaf_pem))
    assert resp.status_code == 200
    data = resp.json()
    assert data["valid"] is True, data
    assert data["error"] is None
    # 链应从叶到根：叶、中间 CA、根 CA
    assert len(data["chain_subjects"]) == 3
    assert "CN=www.example.com" in data["chain_subjects"][0]
    assert "CN=Test Intermediate CA" in data["chain_subjects"][1]
    assert "CN=Test Root CA" in data["chain_subjects"][2]


def test_expired_leaf_rejected(client, pki):
    resp = client.post("/api/v1/verify", json=_payload(pki, pki.expired_leaf_pem))
    data = resp.json()
    assert data["valid"] is False
    assert data["error"]


def test_expired_leaf_accepted_at_past_validation_time(client, pki):
    """指定过去的验证时刻，过期证书当时仍在有效期内，应通过。"""
    past = (datetime.now(timezone.utc) - timedelta(days=200)).isoformat()
    resp = client.post(
        "/api/v1/verify",
        json=_payload(pki, pki.expired_leaf_pem, validation_time=past),
    )
    data = resp.json()
    assert data["valid"] is True, data


def test_non_ca_intermediate_rejected(client, pki):
    """中间证书不是 CA（BasicConstraints CA=FALSE）时，链必须被拒绝。"""
    resp = client.post(
        "/api/v1/verify",
        json=_payload(pki, pki.leaf_under_non_ca_pem, intermediates=[pki.non_ca_pem]),
    )
    data = resp.json()
    assert data["valid"] is False
    assert data["error"]


def test_dns_name_mismatch_rejected(client, pki):
    resp = client.post("/api/v1/verify", json=_payload(pki, pki.mismatch_leaf_pem))
    data = resp.json()
    assert data["valid"] is False
    assert data["error"]


def test_path_length_exceeded_rejected(client, pki):
    """中间 CA pathlen=0，其下再挂一级子 CA 必须被拒绝。"""
    resp = client.post(
        "/api/v1/verify",
        json=_payload(
            pki,
            pki.leaf_under_sub_ca_pem,
            intermediates=[pki.inter_pem, pki.sub_ca_pem],
        ),
    )
    data = resp.json()
    assert data["valid"] is False
    assert data["error"]


def test_untrusted_self_signed_rejected(client, pki):
    """非信任的自签证书不能被当作根接受。"""
    resp = client.post(
        "/api/v1/verify",
        json=_payload(pki, pki.untrusted_self_signed_pem, intermediates=[]),
    )
    data = resp.json()
    assert data["valid"] is False
    assert data["error"]


def test_self_signed_not_in_roots_rejected_even_as_only_candidate(client, pki):
    """把自签证书同时作为叶和候选中间证书，但不在信任根中，仍须拒绝。"""
    resp = client.post(
        "/api/v1/verify",
        json=_payload(
            pki,
            pki.untrusted_self_signed_pem,
            intermediates=[pki.untrusted_self_signed_pem],
        ),
    )
    data = resp.json()
    assert data["valid"] is False


def test_missing_intermediate_rejected(client, pki):
    """缺少中间证书时无法构链到信任根。"""
    resp = client.post(
        "/api/v1/verify", json=_payload(pki, pki.leaf_pem, intermediates=[])
    )
    data = resp.json()
    assert data["valid"] is False


def test_wrong_trust_root_rejected(client, pki):
    """信任根换成另一张不相关的自签证书时，正常链也应失败。"""
    resp = client.post(
        "/api/v1/verify",
        json=_payload(pki, pki.leaf_pem, roots=[pki.untrusted_self_signed_pem]),
    )
    data = resp.json()
    assert data["valid"] is False


def test_malformed_pem_returns_input_error(client, pki):
    resp = client.post(
        "/api/v1/verify", json=_payload(pki, "not-a-pem-certificate")
    )
    data = resp.json()
    assert data["valid"] is False
    assert "输入错误" in data["error"]


def test_empty_dns_name_returns_input_error(client, pki):
    resp = client.post("/api/v1/verify", json=_payload(pki, pki.leaf_pem, dns=""))
    data = resp.json()
    assert data["valid"] is False
    assert "输入错误" in data["error"]
