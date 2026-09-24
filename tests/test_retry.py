"""Acceptance: the retry rule for failed target calls.

Rule implemented by MultisigTimelock.execute:
  * first attempt is allowed at approvedAt + timelockSeconds;
  * a failed target call does NOT revert the outer transaction - the op stays
    Scheduled with failures += 1;
  * every later attempt is allowed at lastAttemptAt + retryCooldownSeconds and
    rejected before that (RetryCooldownActive);
  * after maxFailures failed attempts the op becomes terminal Failed;
  * a success flips the op to terminal Executed exactly once.
"""

from tests.conftest import flaky_successes, now_of
from tests.conftest import selector

COOLDOWN = selector("RetryCooldownActive(uint256,uint256)")
TERMINAL = selector("AlreadyTerminal(bytes32,uint8)")


def _flaky_data(client):
    flaky = client.app.state.service.chain.contract(
        "FlakyTarget", client.app.state.service.targets["flaky"]
    )
    return flaky.encode_abi("run", args=[])


def _propose_flaky(client, indices=(0, 1)):
    deadline = now_of(client) + 3600
    data = _flaky_data(client)
    r = client.post("/operations/propose", json={
        "target": client.app.state.service.targets["flaky"],
        "data": data,
        "deadline": deadline,
        "signer_indices": list(indices),
    })
    return r.json(), data, deadline


def _execute(client, target, data, nonce, deadline):
    return client.post("/operations/execute", json={
        "target": target, "data": data, "nonce": nonce, "deadline": deadline,
    })


def test_failure_then_retry_after_cooldown_succeeds_once(env):
    client = env.client
    body, data, deadline = _propose_flaky(client)
    op_id = body["op_id"]

    client.post("/dev/warp", json={"seconds": 2})

    # Attempt 1: target reverts, outer tx succeeds and records the failure.
    r1 = _execute(client, env.deploy["targets"]["flaky"], data, 0, deadline)
    assert r1.status_code == 200
    assert r1.json()["action"] == "failed_retryable"
    assert r1.json()["op"]["state"] == "Scheduled"
    assert r1.json()["op"]["failures"] == 1

    # Retry before cooldown -> 400.
    early = _execute(client, env.deploy["targets"]["flaky"], data, 0, deadline)
    assert early.status_code == 400
    assert COOLDOWN in early.json()["detail"]["revert"]

    # Repair the target (external switch), wait out the 1s cooldown.
    client.post("/dev/flaky", json={"failing": False})
    client.post("/dev/warp", json={"seconds": 1})
    r2 = _execute(client, env.deploy["targets"]["flaky"], data, 0, deadline)
    assert r2.status_code == 200
    assert r2.json()["action"] == "executed"
    assert flaky_successes(client) == 1

    # Exactly once even after a successful retry.
    again = _execute(client, env.deploy["targets"]["flaky"], data, 0, deadline)
    assert again.status_code == 400
    assert TERMINAL in again.json()["detail"]["revert"]
    assert flaky_successes(client) == 1
    assert client.get(f"/operations/{op_id}").json()["state"] == "Executed"


def test_max_failures_exhausted_is_terminal(env):
    client = env.client
    body, data, deadline = _propose_flaky(client)
    op_id = body["op_id"]
    target = env.deploy["targets"]["flaky"]

    client.post("/dev/warp", json={"seconds": 2})
    r1 = _execute(client, target, data, 0, deadline)
    assert r1.json()["op"]["failures"] == 1
    assert r1.json()["op"]["state"] == "Scheduled"

    # Second failed attempt exhausts maxFailures=2 -> terminal Failed.
    client.post("/dev/warp", json={"seconds": 1})
    r2 = _execute(client, target, data, 0, deadline)
    assert r2.status_code == 200
    assert r2.json()["action"] == "retries_exhausted"
    assert r2.json()["op"]["state"] == "Failed"

    # The repair arrives too late: a Failed op can never be retried.
    client.post("/dev/flaky", json={"failing": False})
    client.post("/dev/warp", json={"seconds": 10})
    r3 = _execute(client, target, data, 0, deadline)
    assert r3.status_code == 400
    assert TERMINAL in r3.json()["detail"]["revert"]
    assert flaky_successes(client) == 0
    assert client.get(f"/operations/{op_id}").json()["state"] == "Failed"
