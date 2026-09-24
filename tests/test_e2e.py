"""End-to-end tests: real Anvil chain + FastAPI HTTP layer + on-chain contract.

Each test boots a fresh Anvil instance and uvicorn server on throwaway ports,
so accounts always start unopened. The acceptance criteria are verified over
HTTP (and, for the second account, via web3.py directly):

* settling once across 100 blocks == settling block-by-block (fees AND dust)
* zero principal, minimal rate, max values
* rounding error bound (< 1 base unit, dust carried not lost)
* no double counting of fees
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest
import requests
from web3 import Web3

ROOT = Path(__file__).resolve().parent.parent
ARTIFACT = ROOT / "out" / "LosslessFeeSettlement.sol" / "LosslessFeeSettlement.json"

# Anvil deterministic dev accounts (well-known test keys, local only).
KEY_A = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
KEY_B = "0x59c6995e998f97a5a0044966f0945389dc9e86dae88c7a8412f4603b6b78690d"
SCALE = 10**18


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture()
def chain():
    """Fresh Anvil + uvicorn per test; yields (base_url, w3, operator_address)."""
    anvil = shutil.which("anvil") or str(Path.home() / ".foundry/bin/anvil")
    anvil_port = _free_port()
    anvil_proc = subprocess.Popen(
        [anvil, "--port", str(anvil_port), "--silent"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    rpc = f"http://127.0.0.1:{anvil_port}"
    w3 = Web3(Web3.HTTPProvider(rpc))
    for _ in range(100):
        if w3.is_connected():
            break
        time.sleep(0.1)
    else:
        anvil_proc.kill()
        pytest.fail("anvil did not start")

    api_port = _free_port()
    env = dict(os.environ, RPC_URL=rpc, PRIVATE_KEY=KEY_A)
    api_proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(api_port)],
        cwd=ROOT,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    base = f"http://127.0.0.1:{api_port}"
    for _ in range(150):
        try:
            r = requests.get(base + "/health", timeout=1)
            if r.ok and r.json()["connected"] and r.json()["contract"]:
                break
        except Exception:
            pass
        time.sleep(0.2)
    else:
        api_proc.kill()
        anvil_proc.kill()
        pytest.fail("uvicorn did not start")

    operator = requests.get(base + "/health").json()["operator"]
    yield base, w3, operator

    api_proc.terminate()
    anvil_proc.terminate()
    api_proc.wait(timeout=10)
    anvil_proc.wait(timeout=10)


# ------------------------------- helpers -----------------------------------


def mine(base, n):
    r = requests.post(base + f"/admin/mine?blocks={n}")
    assert r.ok, r.text
    return r.json()["block"]


def open_account(base, operator, principal, rate):
    r = requests.post(
        base + "/accounts",
        json={"address": operator, "principal": principal, "ratePerBlock": rate},
    )
    assert r.status_code == 201, r.text
    return r.json()


def settle(base, operator):
    r = requests.post(base + "/settle", json={"address": operator})
    assert r.ok, r.text
    return r.json()


def get(base, address):
    r = requests.get(base + f"/accounts/{address}")
    assert r.ok, r.text
    return r.json()


def ref_settle(p, r, n, carry):
    """Python big-int reference of the on-chain integer formula."""
    per = (p * r) // SCALE
    rem = (p * r) % SCALE
    t = rem * n + carry
    return per * n + t // SCALE, t % SCALE


def web3_account(w3, contract_address, key):
    artifact = json.loads(ARTIFACT.read_text())
    acct = w3.eth.account.from_key(key)
    c = w3.eth.contract(address=contract_address, abi=artifact["abi"])

    def send(fn):
        tx = fn.build_transaction(
            {
                "from": acct.address,
                "nonce": w3.eth.get_transaction_count(acct.address),
                "gas": 3_000_000,
            }
        )
        signed = acct.sign_transaction(tx)
        rcpt = w3.eth.wait_for_transaction_receipt(
            w3.eth.send_raw_transaction(signed.raw_transaction)
        )
        assert rcpt["status"] == 1
        return rcpt

    return c, acct, send


# --------------------------------------------------------------------------
# Acceptance: one settle across 100 blocks == 100 per-block settles
# --------------------------------------------------------------------------


def test_100_blocks_batch_equals_stepwise(chain):
    base, w3, operator = chain
    p, rate = 1_000 * SCALE, 3 * 10**15  # 0.3%/block, dust-producing

    contract_addr = requests.get(base + "/health").json()["contract"]
    c, acct_b, send_b = web3_account(w3, contract_addr, KEY_B)

    # Anvil mines one block per transaction. Layout (B opens at block L,
    # A at L+1):
    #   * B settles 99 times in blocks L+2..L+100 -> first settle spans
    #     2 blocks (A's open took L+1), then 98 single blocks: total 100.
    #   * A then settles once in block L+101: span = (L+101) - (L+1) = 100.
    # Both paths cover exactly 100 blocks with identical (p, rate), so the
    # carry identity makes them exactly equal.
    send_b(c.functions.open(p, rate))
    open_account(base, operator, p, rate)

    k = 99  # B settles 99 times -> 100 blocks of coverage
    for _ in range(k):
        send_b(c.functions.settle())
    step_fee = c.functions.getAccount(acct_b.address).call()[3]
    step_rem = c.functions.getAccount(acct_b.address).call()[4]

    acc_a_before = get(base, operator)
    assert acc_a_before["pendingBlocks"] == k  # 99 now; settle tx adds the 100th
    batch = settle(base, operator)
    batch_fee = int(batch["feeAdded"])
    acc_a = get(base, operator)

    assert batch_fee == step_fee, "batch fee must equal stepwise fee"
    assert batch_fee > 0
    assert int(acc_a["remainder"]) == step_rem, "carried remainder must match"
    # Independent big-integer reference over the same 100-block span
    exp_fee, exp_rem = ref_settle(p, rate, 100, 0)
    assert batch_fee == exp_fee
    assert int(acc_a["remainder"]) == exp_rem
    # Rounding bound: remainder strictly below one base unit
    assert 0 <= int(acc_a["remainder"]) < SCALE


# --------------------------------------------------------------------------
# Edge cases
# --------------------------------------------------------------------------


def test_zero_principal(chain):
    base, _, operator = chain
    open_account(base, operator, 0, 5 * 10**16)
    mine(base, 100)
    res = settle(base, operator)
    assert res["feeAdded"] == "0"
    acc = get(base, operator)
    assert acc["accruedFees"] == "0"
    assert acc["remainder"] == "0"
    # Nothing was ever due: claim reverts with NothingDue -> HTTP 422.
    r = requests.post(base + "/claim", json={"address": operator})
    assert r.status_code == 422


def test_minimal_rate_dust_promotes(chain):
    base, _, operator = chain
    # Pure-formula check at the extreme: 1e9 blocks of 1e-9 fee -> exactly 1.
    q = requests.get(
        base + "/quote",
        params={"principal": 10**9, "ratePerBlock": 1, "blocks": 10**9, "carry": 0},
    ).json()
    assert q["feeAdded"] == "1"
    assert q["carryOut"] == "0"

    # Live run with dust of 1e15/block. The settle tx itself occupies a
    # block, so mine(998) -> settle spans 999 blocks (fee 0, pure carry);
    # the next settle (its own tx block) spans block #1000 exactly:
    # 1000 * 1e15 = 1e18 -> exactly one base unit, remainder back to 0.
    open_account(base, operator, 10**15, 1)  # rate = 1e-18/block
    mine(base, 998)
    res = settle(base, operator)
    assert res["feeAdded"] == "0"
    acc = get(base, operator)
    assert acc["remainder"] == str(999 * 10**15)  # dust carried, not lost
    res = settle(base, operator)  # no mine: the tx itself is block #1000
    assert res["feeAdded"] == "1"
    acc = get(base, operator)
    assert acc["remainder"] == "0"


def test_max_values(chain):
    base, _, operator = chain
    max256 = 2**256 - 1
    # Max principal with tiny rate: P*R overflows 256 bits -> 512-bit mulDiv.
    open_account(base, operator, max256, 10**9)  # 1e-9 per block
    mine(base, 100)
    # The settle tx occupies a block itself: span = pendingBlocks + 1.
    n = get(base, operator)["pendingBlocks"] + 1
    res = settle(base, operator)
    exp_fee, exp_rem = ref_settle(max256, 10**9, n, 0)
    assert int(res["feeAdded"]) == exp_fee
    acc = get(base, operator)
    assert int(acc["remainder"]) == exp_rem
    assert 0 <= int(acc["remainder"]) < SCALE


def test_no_double_counting(chain):
    base, _, operator = chain
    open_account(base, operator, 500, 10**17)  # 50/block exact
    mine(base, 3)
    n = get(base, operator)["pendingBlocks"] + 1  # settle tx adds one block
    first = settle(base, operator)
    assert first["feeAdded"] == str(50 * n)
    # A second settle covers ONLY its own (new) tx block: past blocks are
    # never re-counted. Total accrued == exact fee for the total span.
    second = settle(base, operator)
    assert second["feeAdded"] == "50"  # exactly one new block, not n again
    acc = get(base, operator)
    assert int(acc["accruedFees"]) == 50 * (n + 1)
    # Claim zeroes the balance (view call: no new block, still 0).
    r = requests.post(base + "/claim", json={"address": operator})
    assert r.ok, r.text
    assert r.json()["accruedFees"] == "0"
    # A follow-up claim only pays what accrued in its own tx block (50) —
    # the already-claimed n+1 blocks are never paid twice.
    r = requests.post(base + "/claim", json={"address": operator})
    assert r.ok, r.text
    acc = get(base, operator)
    assert acc["accruedFees"] == "0"


def test_rate_change_settles_old_regime(chain):
    base, _, operator = chain
    open_account(base, operator, 1000 * SCALE, 10**16)  # 1%/block
    mine(base, 10)
    before = get(base, operator)
    n = before["pendingBlocks"] + 1  # the setRate tx occupies a block itself
    r = requests.post(base + "/rate", json={"address": operator, "ratePerBlock": 2 * 10**16})
    assert r.ok, r.text
    body = r.json()
    exp_old, exp_rem = ref_settle(1000 * SCALE, 10**16, n, 0)
    assert int(body["accruedFees"]) == exp_old
    assert int(body["remainder"]) == exp_rem
    assert body["ratePerBlock"] == str(2 * 10**16)

    mine(base, 10)
    n2 = get(base, operator)["pendingBlocks"] + 1  # settle tx adds one block
    res = settle(base, operator)
    exp_new, _ = ref_settle(1000 * SCALE, 2 * 10**16, n2, exp_rem)
    assert int(res["feeAdded"]) == exp_new


def test_rounding_error_bound(chain):
    """Lifetime settled fee is always within one base unit of the exact real
    fee; the shortfall lives in the remainder, never vanishes."""
    base, _, operator = chain
    p, rate = 7, 10**17  # 0.7/block
    open_account(base, operator, p, rate)
    mine(base, 10)
    n = get(base, operator)["pendingBlocks"] + 1  # settle tx adds one block
    res = settle(base, operator)
    acc = get(base, operator)
    exact_num = p * rate * n  # exact fee = exact_num / SCALE
    settled = int(res["feeAdded"])
    rem = int(acc["remainder"])
    assert settled * SCALE + rem == exact_num  # lossless decomposition
    assert exact_num - settled * SCALE < SCALE  # error < 1 base unit
