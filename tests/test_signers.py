"""Acceptance: signer-set changes invalidate in-flight ops and old signatures,
and externally produced signatures (handled over the digest endpoint) work.
"""

from eth_account import Account

from service.signing import sign_op
from tests.conftest import counter_count, counter_data, now_of
from tests.conftest import selector

BAD_SIG = selector("BadSignature(uint256)")
TERMINAL = selector("AlreadyTerminal(bytes32,uint8)")
THRESHOLD = selector("ThresholdNotMet(uint256,uint256)")


def test_change_signers_invalidates_inflight_and_old_key(env):
    client = env.client
    target = env.deploy["targets"]["counter"]

    # Schedule op nonce 0 under the initial {s0,s1,s2}/2 set.
    body = client.post("/operations/propose", json={
        "target": target, "data": counter_data(client, 1, b"pre"),
        "deadline": now_of(client) + 3600, "signer_indices": [0, 1],
    }).json()
    data = counter_data(client, 1, b"pre")
    deadline0 = body["op"]["deadline"]
    old_id = body["op_id"]
    assert body["op"]["state"] == "Scheduled"

    # Governance op nonce 1: new set {s1,s2,s3}, threshold 3.
    from service.config import ANVIL_KEYS
    new_set = [Account.from_key(ANVIL_KEYS[i]).address for i in (1, 2, 3)]

    gov = client.post("/admin/signers", json={
        "signers": new_set,
        "threshold": 3,
        "deadline": now_of(client) + 3600,
        "signer_indices": [0, 1],
    })
    assert gov.status_code == 200
    state = client.get("/state").json()
    assert state["threshold"] == 3
    assert state["config_version"] == 1
    assert state["nonce"] == 2

    # The in-flight op is terminal Invalidated and never executes.
    op = client.get(f"/operations/{old_id}").json()
    assert op["state"] == "Invalidated"
    client.post("/dev/warp", json={"seconds": 2})
    r = client.post("/operations/execute", json={
        "target": target, "data": data, "nonce": 0, "deadline": deadline0,
    })
    assert r.status_code == 400
    assert TERMINAL in r.json()["detail"]["revert"]
    assert counter_count(client) == 0

    # A fresh op signed by the removed key s0 is rejected (BadSignature)...
    d2 = counter_data(client, 2, b"post")
    deadline2 = now_of(client) + 3600
    stale_sigs = [
        sign_op(ANVIL_KEYS[i], 31337, env.deploy["executor"],
                target, 0, d2, 2, deadline2, 1)
        for i in (0, 1, 2)
    ]
    stale = client.post("/operations/propose", json={
        "target": target, "data": d2,
        "deadline": deadline2, "signatures": stale_sigs,
    })
    assert stale.status_code == 400
    assert BAD_SIG in stale.json()["detail"]["revert"]

    # ...while 3 signatures from the new set schedule and execute.
    sigs = [
        sign_op(ANVIL_KEYS[i], 31337, env.deploy["executor"],
                target, 0, d2, 2, deadline2, 1)
        for i in (1, 2, 3)
    ]
    ok = client.post("/operations/propose", json={
        "target": target, "data": d2,
        "deadline": deadline2, "signatures": sigs,
    })
    assert ok.status_code == 200, ok.text
    assert ok.json()["op"]["state"] == "Scheduled"
    client.post("/dev/warp", json={"seconds": 2})
    executed = client.post("/operations/execute", json={
        "target": target, "data": d2, "nonce": 2, "deadline": deadline2,
    })
    assert executed.status_code == 200
    assert counter_count(client) == 2


def test_change_signers_below_threshold_rejected(env):
    client = env.client
    from service.config import ANVIL_KEYS
    new_set = [Account.from_key(ANVIL_KEYS[i]).address for i in (1, 2, 3)]
    r = client.post("/admin/signers", json={
        "signers": new_set,
        "threshold": 3,
        "deadline": now_of(client) + 3600,
        "signer_indices": [0],  # only 1 of required 2
    })
    assert r.status_code == 400
    assert THRESHOLD in r.json()["detail"]["revert"]
    assert client.get("/state").json()["config_version"] == 0


def test_external_signature_workflow_via_digest_endpoint(env):
    """An external signer fetches the digest, signs off-service, and submits
    the raw signature through the API."""
    client = env.client
    target = env.deploy["targets"]["counter"]
    data = counter_data(client, 9, b"ext")
    deadline = now_of(client) + 3600

    info = client.get("/operations/digest", params={
        "target": target, "data": data, "nonce": 0, "deadline": deadline,
    }).json()

    from service.config import ANVIL_KEYS
    # Sign with two configured signers in reverse order.
    sigs = [
        sign_op(ANVIL_KEYS[2], 31337, env.deploy["executor"],
                target, 0, data, 0, deadline, info["config_version"]),
        sign_op(ANVIL_KEYS[0], 31337, env.deploy["executor"],
                target, 0, data, 0, deadline, info["config_version"]),
    ]
    r = client.post("/operations/propose", json={
        "target": target, "data": data, "deadline": deadline,
        "signatures": sigs,
    })
    assert r.status_code == 200, r.text
    assert r.json()["op_id"] == info["op_id"]
    assert r.json()["op"]["state"] == "Scheduled"

    # A garbage signature is rejected with a clean error.
    bad = client.post("/operations/approve", json={
        "target": target, "data_hash": info["data_hash"],
        "nonce": 0, "deadline": deadline,
        "signatures": ["0x" + "ab" * 65],
    })
    assert bad.status_code == 400
