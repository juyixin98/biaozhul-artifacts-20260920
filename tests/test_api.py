"""HTTP endpoint tests via FastAPI TestClient."""

# ------------------------------------------------------------------- /healthz
def test_healthz(client, strict_client):
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"
    assert r.json()["unsigned_allowed"] is True
    r2 = strict_client.get("/healthz")
    assert r2.json()["unsigned_allowed"] is False


# ----------------------------------------------------------------- /evaluate
def test_evaluate_unsigned_permit(client, demo_policy):
    r = client.post("/v1/evaluate", json={
        "policy": demo_policy,
        "subject": {"user": "alice", "type": "employee",
                    "verified": True, "clearance": 4},
        "resource": {"owner": "alice", "action": "read",
                     "classification": "internal", "tags": ["draft"]},
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["decision"] == "permit"
    # alice is both the owner and a verified employee: both matched permits
    # belong to the deciding set.
    assert body["relevant_rules"] == [
        "permit-owner", "permit-verified-employee"
    ]
    assert body["order_check"]["order_invariant"] is True
    # trace exposes per-rule true/false/unknown
    conds = {t["rule_id"]: t["condition"] for t in body["traces"]}
    assert conds["deny-contractors"] == "false"


def test_evaluate_unknown_attribute_default_denies(client, demo_policy):
    r = client.post("/v1/evaluate", json={
        "policy": demo_policy,
        "subject": {"user": "x"},
        "resource": {"action": "read"},
    })
    assert r.status_code == 200
    body = r.json()
    assert body["decision"] == "deny"
    assert body["reason"] == "default_deny_no_match"
    assert body["relevant_rules"] == []


def test_strict_client_rejects_unsigned(strict_client, demo_policy):
    r = strict_client.post("/v1/evaluate", json={
        "policy": demo_policy,
        "subject": {}, "resource": {},
    })
    assert r.status_code == 403
    assert r.json()["error"] == "untrusted_policy"


def test_signed_bundle_accepted_by_strict_client(strict_client, signer,
                                                 demo_policy):
    r = strict_client.post("/v1/evaluate", json={
        "signed": signer(demo_policy),
        "subject": {"user": "alice", "type": "employee",
                    "verified": True, "clearance": 4},
        "resource": {"owner": "alice", "action": "read",
                     "classification": "internal", "tags": []},
    })
    assert r.status_code == 200, r.text
    assert r.json()["decision"] == "permit"


def test_tampered_signed_bundle_rejected(strict_client, signer, demo_policy):
    bundle = signer(demo_policy)
    bundle["document"]["rules"][0]["effect"] = "permit"
    r = strict_client.post("/v1/evaluate", json={
        "signed": bundle,
        "subject": {}, "resource": {},
    })
    assert r.status_code == 403
    assert r.json()["error"] == "untrusted_policy"


def test_neither_policy_nor_signed_is_400(client):
    r = client.post("/v1/evaluate", json={"subject": {}})
    assert r.status_code == 400
    assert r.json()["error"] == "invalid_request"


def test_invalid_policy_shape_is_422(client):
    r = client.post("/v1/evaluate", json={
        "policy": {"id": "p", "rules": [
            {"id": "r", "effect": "maybe", "when": {"op": "lit", "value": True}}
        ]},
    })
    assert r.status_code == 422
    assert r.json()["error"] == "invalid_policy"


def test_unknown_top_level_field_is_422(client):
    r = client.post("/v1/evaluate", json={
        "policy": {"id": "p", "rules": []},
        "subject": {},
        "inject": True,
    })
    assert r.status_code == 422


def test_type_clash_is_422(client):
    r = client.post("/v1/evaluate", json={
        "policy": {"id": "p", "rules": [
            {"id": "r", "effect": "permit",
             "when": {"op": "lt",
                      "left": {"op": "attr", "bag": "subject", "key": "n"},
                      "right": {"op": "lit", "value": "3"}}}
        ]},
        "subject": {"n": 3},
    })
    assert r.status_code == 422
    assert r.json()["error"] == "evaluation_error"


def test_body_too_large_rejected():
    from app.signing import TrustStore
    from app.main import create_app
    from fastapi.testclient import TestClient

    small_app = create_app(TrustStore(), allow_unsigned=True,
                           max_body_bytes=128)
    c = TestClient(small_app)
    r = c.post("/v1/evaluate", content=b"[" + b"0," * 500 + b"0]",
               headers={"content-type": "application/json"})
    assert r.status_code == 413


# -------------------------------------------------------- /v1/evaluate-policy-set
def test_policy_set_conflict_endpoint(client, demo_policy_set):
    r = client.post("/v1/evaluate-policy-set", json={
        "policies": demo_policy_set["policies"],
        "subject": {"role": "admin", "region": "US", "groups": ["eng"]},
        "resource": {"group": "eng", "action": "read", "region": "EU"},
    })
    assert r.status_code == 200
    body = r.json()
    assert body["decision"] == "deny"
    assert body["conflict"] is True
    assert body["relevant_rules"] == ["region-policy:deny-export-us"]
    assert body["order_check"]["order_invariant"] is True


def test_signed_policy_set_on_single_policy_endpoint_is_400(
        strict_client, signer, demo_policy_set):
    r = strict_client.post("/v1/evaluate", json={
        "signed": signer(demo_policy_set),
    })
    assert r.status_code == 400
    assert "policy-set" in r.json()["message"]


# -------------------------------------------------------------- /v1/truth-table
def test_truth_table_endpoint(client, demo_policy):
    r = client.post("/v1/truth-table", json={
        "policy": demo_policy,
        "mode": "full",
        "variables": [
            {"bag": "resource", "key": "classification",
             "values": ["public", "internal", "secret"]},
            {"bag": "resource", "key": "action",
             "values": ["read", "write"]},
        ],
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["row_count"] == 6
    assert body["order_invariant"] is True
    # public+read permits; writes never permit (no write rules)
    by_key = {
        (row["assignment"][0]["value"], row["assignment"][1]["value"]):
            row["decision"]
        for row in body["rows"]
    }
    assert by_key[("public", "read")] == "permit"
    assert by_key[("public", "write")] == "deny"


def test_truth_table_signed(strict_client, signer, demo_policy):
    r = strict_client.post("/v1/truth-table", json={
        "signed": signer(demo_policy),
        "mode": "ternary",
        "variables": [
            {"bag": "resource", "key": "classification",
             "values": ["public", "internal"]},
        ],
    })
    assert r.status_code == 200
    assert r.json()["row_count"] == 3


def test_truth_table_over_cap_is_400(client, demo_policy):
    r = client.post("/v1/truth-table", json={
        "policy": demo_policy,
        "mode": "full",
        "variables": [
            {"bag": "resource", "key": f"k{i}", "values": ["a", "b"]}
            for i in range(13)
        ],
    })
    assert r.status_code == 400
    assert r.json()["error"] == "invalid_request"
