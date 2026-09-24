"""HTTP-level tests: FastAPI routes, validation, HMAC auth, batch handling."""

import numpy as np
import pytest
from fastapi.testclient import TestClient

from ekf_fusion.app import SESSIONS, app
from ekf_fusion.crypto import (
    canonical_message,
    hmac_sign,
    hmac_verify,
    new_session_id,
    new_session_key,
)

client = TestClient(app)


@pytest.fixture(autouse=True)
def _clear_sessions():
    SESSIONS.clear()
    yield
    SESSIONS.clear()


def mk(mid="m1", t=0.0, kind="gnss", z=None, r=None, sig=None):
    return {
        "message_id": mid, "t": t, "kind": kind,
        "z": z if z is not None else [0.0, 0.0],
        "R": r if r is not None else [[0.04, 0.0], [0.0, 0.04]],
        **({"signature": sig} if sig is not None else {}),
    }


def make_session(require_hmac=False):
    resp = client.post("/sessions", json={"require_hmac": require_hmac})
    assert resp.status_code == 201
    body = resp.json()
    return body["session_id"], body["hmac_key"]


# -------------------------------------------------------------- basic routes

def test_health_and_session_lifecycle():
    assert client.get("/health").json()["status"] == "ok"
    sid, key = make_session()
    assert len(key) == 64  # 32 bytes hex
    assert client.get(f"/sessions/{sid}/state").status_code == 200
    assert client.delete(f"/sessions/{sid}").status_code == 200
    assert client.get(f"/sessions/{sid}/state").status_code == 404


def test_submit_and_step_trace_and_state():
    sid, _ = make_session()
    for t in np.arange(0.0, 1.0, 0.2):
        r = client.post(f"/sessions/{sid}/measurements",
                        json=mk(f"g{t}", float(t), "gnss",
                                [float(t), 0.4 * float(t)]))
        assert r.status_code == 200
        assert r.json()["status"] == "accepted"
    steps = client.get(f"/sessions/{sid}/steps").json()
    assert steps["n"] == 5
    # every step carries state and innovation diagnostics
    s0 = steps["steps"][0]
    assert set(s0) >= {"x", "P", "x_pred", "p_pred", "innovation",
                       "S", "nis", "K", "chain_hash"}
    state = client.get(f"/sessions/{sid}/state").json()
    assert state["psd"] is True
    assert state["min_eigenvalue"] >= -1e-12


def test_unknown_measurement_kind_422():
    sid, _ = make_session()
    bad = mk()
    bad["kind"] = "imu"
    assert client.post(f"/sessions/{sid}/measurements", json=bad).status_code == 422


def test_bad_covariance_rejected_per_item():
    sid, _ = make_session()
    asym = mk("a1", r=[[1.0, 0.5], [0.2, 1.0]])
    out = client.post(f"/sessions/{sid}/measurements", json=asym).json()
    assert out["status"] == "rejected"
    assert out["error"] == "covariance_not_symmetric"

    npsd = mk("a2", r=[[1.0, 2.0], [2.0, 1.0]])
    out = client.post(f"/sessions/{sid}/measurements", json=npsd).json()
    assert out["error"] == "covariance_not_psd"

    # NaN serialised as the (non-standard) JSON token Pydantic accepts;
    # covariance validation must still reject it.
    raw = (
        '{"message_id":"a3","t":0.0,"kind":"gnss","z":[0,0],'
        '"R":[[NaN,0.0],[0.0,1.0]]}'
    )
    resp = client.post(f"/sessions/{sid}/measurements", content=raw,
                       headers={"content-type": "application/json"})
    assert resp.status_code in (200, 422)
    if resp.status_code == 200:
        out = resp.json()
        assert out["error"] == "covariance_non_finite"
    else:
        assert resp.status_code == 422  # strict decoder deployment

    # timeline untouched by structurally invalid input
    state = client.get(f"/sessions/{sid}/state").json()
    assert state["n_ingested"] == 0


def test_late_too_old_and_duplicate_over_http():
    sid, _ = make_session()
    client.post(f"/sessions/{sid}/measurements", json=mk("g0", 0.0))
    client.post(f"/sessions/{sid}/measurements",
                json=mk("g5", 5.0, z=[5.0, 2.0]))
    old = client.post(f"/sessions/{sid}/measurements",
                      json=mk("old", 2.99, z=[0.0, 0.0]))
    body = old.json()
    assert body["status"] == "rejected"
    assert body["reason"] == "late_too_old"

    dup = client.post(f"/sessions/{sid}/measurements",
                      json=mk("g0", 0.5))
    assert dup.json()["reason"] == "duplicate_message_id"


def test_non_finite_time_is_rejected():
    sid, _ = make_session()
    raw = ('{"message_id":"x","t":-Infinity,"kind":"gnss","z":[0,0],'
           '"R":[[0.04,0],[0,0.04]]}')
    resp = client.post(f"/sessions/{sid}/measurements", content=raw,
                       headers={"content-type": "application/json"})
    if resp.status_code == 200:
        assert resp.json()["error"] == "time_non_finite"
    else:
        assert resp.status_code == 422


def test_outlier_rejection_evidence_endpoint():
    sid, _ = make_session()
    for t in np.arange(0.0, 2.0, 0.2):
        client.post(f"/sessions/{sid}/measurements",
                    json=mk(f"g{t}", float(t), "gnss",
                            [float(t), 0.4 * float(t)]))
    blip = client.post(f"/sessions/{sid}/measurements",
                       json=mk("blip", 2.0, "gnss", [500.0, -400.0]))
    assert blip.json()["reason"] == "outlier_gate"
    rej = client.get(f"/sessions/{sid}/rejections").json()["rejections"]
    assert any(r["message_id"] == "blip" and r["nis"] > 9.21 for r in rej)


def test_batch_processes_each_item_independently():
    sid, _ = make_session()
    batch = {"measurements": [
        mk("b1", 0.0, "gnss", [0.0, 0.0]),
        mk("b2", 0.2, "gnss", [0.2, 0.08]),
        mk("bad", 0.4, "gnss", [0.4, 0.16], r=[[1.0, 0.9], [0.1, 1.0]]),
        mk("b3", 0.6, "gnss", [0.6, 0.24]),
    ]}
    out = client.post(f"/sessions/{sid}/measurements/batch", json=batch).json()
    assert out["n_accepted"] == 3
    assert out["n_rejected"] == 1
    reasons = [(x["message_id"], x.get("error") or x.get("reason"))
               for x in out["results"]]
    assert ("bad", "covariance_not_symmetric") in reasons
    assert out["state"]["initialized"] is True


# ------------------------------------------------------- out-of-order over HTTP

def test_http_out_of_order_matches_ordered_session():
    sid_a, _ = make_session()
    sid_b, _ = make_session()
    msgs = []
    for t in np.arange(0.02, 4.0, 0.1):
        tt = round(float(t), 6)
        msgs.append(mk(f"o{tt:.4f}", tt, "odom",
                       [1.0, 0.4], [[0.0025, 0], [0, 0.0025]]))
    for t in np.arange(0.1, 4.0, 0.2):
        tt = round(float(t), 6)
        msgs.append(mk(f"g{tt:.4f}", tt, "gnss",
                       [tt, 0.4 * tt]))
    ordered = sorted(msgs, key=lambda m: (m["t"], 0 if m["kind"] == "odom" else 1))

    # bounded late swaps: displaced items stay well inside the 2 s window
    shuffled = ordered[:]
    shuffled[5], shuffled[15] = shuffled[15], shuffled[5]
    shuffled[10], shuffled[12] = shuffled[12], shuffled[10]

    for m in shuffled:
        resp = client.post(f"/sessions/{sid_a}/measurements", json=m)
        assert resp.json()["status"] == "accepted", resp.text

    for m in ordered:
        client.post(f"/sessions/{sid_b}/measurements", json=m)

    sa = client.get(f"/sessions/{sid_a}/state").json()
    sb = client.get(f"/sessions/{sid_b}/state").json()
    assert sa["x"] == pytest.approx(sb["x"], abs=1e-12)
    assert sa["chain_hash"] == sb["chain_hash"]
    assert client.get(f"/sessions/{sid_a}/integrity").json()["ok"]


# -------------------------------------------------------------- cryptography

def test_hmac_sign_verify_roundtrip_and_tamper_detection():
    key = new_session_key()
    assert len(bytes.fromhex(key)) == 32
    payload = canonical_message("m1", 1.5, "gnss", [1.0, 2.0],
                                [[0.1, 0.0], [0.0, 0.2]])
    sig = hmac_sign(key, payload)
    assert hmac_verify(key, payload, sig)
    assert not hmac_verify(key, payload + b"x", sig)
    assert not hmac_verify(new_session_key(), payload, sig)
    assert not hmac_verify(key, payload, "not-hex-sig")


def test_session_ids_unique_and_unpredictable():
    ids = {new_session_id() for _ in range(100)}
    assert len(ids) == 100


def test_hmac_enforced_session_rejects_bad_signature_accepts_good():
    sid, key = make_session(require_hmac=True)

    # missing signature
    r = client.post(f"/sessions/{sid}/measurements", json=mk("m1"))
    assert r.status_code == 401
    assert r.json()["detail"]["error"] == "invalid_signature"

    # wrong signature
    bad = mk("m1", sig="00" * 32)
    r = client.post(f"/sessions/{sid}/measurements", json=bad)
    assert r.status_code == 401

    # valid signature over the exact canonical bytes
    m = mk("m1", 0.0, "gnss", [0.0, 0.0])
    payload = canonical_message(m["message_id"], m["t"], m["kind"], m["z"], m["R"])
    m["signature"] = hmac_sign(key, payload)
    r = client.post(f"/sessions/{sid}/measurements", json=m)
    assert r.status_code == 200
    assert r.json()["status"] == "accepted"

    # a signature computed after mutating the measurement must fail
    m2 = mk("m2", 1.0, "gnss", [1.0, 0.4])
    m2["signature"] = hmac_sign(
        key, canonical_message("m2", 1.0, "gnss", [9.0, 9.0], m2["R"]))
    r = client.post(f"/sessions/{sid}/measurements", json=m2)
    assert r.status_code == 401


def test_batch_with_hmac_marks_unauthenticated_items():
    sid, key = make_session(require_hmac=True)
    good = mk("g1", 0.0)
    good["signature"] = hmac_sign(
        key, canonical_message("g1", 0.0, "gnss", good["z"], good["R"]))
    bad = mk("g2", 0.2, sig="11" * 32)
    out = client.post(f"/sessions/{sid}/measurements/batch",
                      json={"measurements": [good, bad]}).json()
    assert out["n_accepted"] == 1
    assert out["results"][1]["error"] == "invalid_signature"
