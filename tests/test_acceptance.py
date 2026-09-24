"""Acceptance: unordered/duplicate signatures, timelock, at-most-once,
tamper binding and deadlines - driven entirely over HTTP.
"""

from tests.conftest import counter_count, counter_data, now_of, propose_counter, selector

DUP = selector("DuplicateSignature(address)")
BAD_SIG = selector("BadSignature(uint256)")
TERMINAL = selector("AlreadyTerminal(bytes32,uint8)")
NONCE_USED = selector("NonceUsed(uint64)")
TIMLOCK = selector("TimelockActive(uint256,uint256)")
DEADLINE = selector("DeadlinePassed(uint256)")


def test_health_and_state(env):
    r = env.client.get("/health")
    assert r.status_code == 200
    assert r.json()["chain_id"] == 31337

    state = env.client.get("/state").json()
    assert state["threshold"] == 2
    assert len(state["signers"]) == 3
    assert state["nonce"] == 0
    assert state["config_version"] == 0


def test_unordered_signatures_schedule_and_execute_once(env):
    client = env.client
    body, data, deadline = propose_counter(client, step=5, indices=(2, 0))  # scrambled
    assert body["op"]["state"] == "Scheduled"
    assert body["op"]["approval_count"] == 2
    assert body["op"]["nonce"] == 0
    op_id = body["op_id"]

    # Timelock not elapsed -> 400 TimelockActive.
    early = client.post("/operations/execute", json={
        "target": env.deploy["targets"]["counter"], "data": data,
        "nonce": 0, "deadline": deadline,
    })
    assert early.status_code == 400
    assert TIMLOCK in early.json()["detail"]["revert"]

    # Fast-forward past the 2-second timelock and execute.
    client.post("/dev/warp", json={"seconds": 2})
    ok = client.post("/operations/execute", json={
        "target": env.deploy["targets"]["counter"], "data": data,
        "nonce": 0, "deadline": deadline,
    })
    assert ok.status_code == 200
    assert ok.json()["action"] == "executed"
    assert counter_count(client) == 5

    # Exactly once: a second execution is terminal.
    again = client.post("/operations/execute", json={
        "target": env.deploy["targets"]["counter"], "data": data,
        "nonce": 0, "deadline": deadline,
    })
    assert again.status_code == 400
    assert TERMINAL in again.json()["detail"]["revert"]
    assert client.get(f"/operations/{op_id}").json()["state"] == "Executed"
    assert counter_count(client) == 5


def test_duplicate_signature_in_single_batch_rejected(env):
    client = env.client
    deadline = now_of(client) + 3600
    r = client.post("/operations/propose", json={
        "target": env.deploy["targets"]["counter"],
        "data": counter_data(client),
        "deadline": deadline,
        "signer_indices": [0, 0],
    })
    assert r.status_code == 400
    assert DUP in r.json()["detail"]["revert"]


def test_duplicate_signature_across_separate_calls_rejected(env):
    client = env.client
    deadline = now_of(client) + 3600

    first = client.post("/operations/propose", json={
        "target": env.deploy["targets"]["counter"],
        "data": counter_data(client, tag=b"a"),
        "deadline": deadline,
        "signer_indices": [0],
    })
    assert first.status_code == 200
    assert first.json()["op"]["state"] == "Proposed"
    target = env.deploy["targets"]["counter"]
    data_hash = first.json()["op"]["data_hash"]

    # Same signer again -> DuplicateSignature.
    dup = client.post("/operations/approve", json={
        "target": target, "data_hash": data_hash,
        "nonce": 0, "deadline": deadline, "signer_indices": [0],
    })
    assert dup.status_code == 400
    assert DUP in dup.json()["detail"]["revert"]

    # A different signer reaches threshold -> Scheduled.
    second = client.post("/operations/approve", json={
        "target": target, "data_hash": data_hash,
        "nonce": 0, "deadline": deadline, "signer_indices": [1],
    })
    assert second.status_code == 200
    assert second.json()["op"]["state"] == "Scheduled"


def test_signature_from_non_signer_rejected(env):
    client = env.client
    from service.config import ANVIL_KEYS
    from service.signing import sign_op

    target = env.deploy["targets"]["counter"]
    data = counter_data(client, tag=b"n")
    deadline = now_of(client) + 3600
    # One real signer (s0) plus a key outside the signer set (Anvil key 4).
    sigs = [
        sign_op(ANVIL_KEYS[0], 31337, env.deploy["executor"],
                target, 0, data, 0, deadline, 0),
        sign_op(ANVIL_KEYS[4], 31337, env.deploy["executor"],
                target, 0, data, 0, deadline, 0),
    ]
    r = client.post("/operations/propose", json={
        "target": target,
        "data": data,
        "deadline": deadline,
        "signatures": sigs,
    })
    assert r.status_code == 400
    assert BAD_SIG in r.json()["detail"]["revert"]


def test_nonce_is_claimed_when_threshold_met(env):
    """Over HTTP the service always reads the on-chain next-nonce: once an op
    schedules, nonce advances so the next proposal uses nonce 1. Stale-nonce
    rejection itself is covered by the Foundry tests."""
    client = env.client
    propose_counter(client, step=1, indices=(0, 1))
    assert client.get("/state").json()["nonce"] == 1

    body, _, _ = propose_counter(client, step=2, indices=(0, 1))
    assert body["op"]["nonce"] == 1
    assert client.get("/state").json()["nonce"] == 2


def test_tampered_calldata_is_unknown_op(env):
    client = env.client
    body, data, deadline = propose_counter(client, step=1)
    client.post("/dev/warp", json={"seconds": 2})

    tampered = counter_data(client, step=999, tag=b"http")
    r = client.post("/operations/execute", json={
        "target": env.deploy["targets"]["counter"], "data": tampered,
        "nonce": 0, "deadline": deadline,
    })
    assert r.status_code == 400
    assert "UnknownOp" in r.json()["detail"]["revert"]
    assert counter_count(client) == 0

    # Original payload still executes fine.
    ok = client.post("/operations/execute", json={
        "target": env.deploy["targets"]["counter"], "data": data,
        "nonce": 0, "deadline": deadline,
    })
    assert ok.status_code == 200
    assert counter_count(client) == 1


def test_execution_after_deadline_refused_then_void(env):
    client = env.client
    body, data, deadline = propose_counter(client, step=1)
    op_id = body["op_id"]

    client.post("/dev/warp", json={"seconds": deadline - now_of(client) + 1})
    late = client.post("/operations/execute", json={
        "target": env.deploy["targets"]["counter"], "data": data,
        "nonce": 0, "deadline": deadline,
    })
    assert late.status_code == 400
    assert DEADLINE in late.json()["detail"]["revert"]
    assert counter_count(client) == 0

    # Expired op can be marked Void and then never runs.
    voided = client.post(f"/operations/{op_id}/void")
    assert voided.status_code == 200
    assert voided.json()["op"]["state"] == "Void"
    assert counter_count(client) == 0
