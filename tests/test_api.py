"""End-to-end acceptance tests through the HTTP API against local anvil.

Block-counting conventions (anvil automines every transaction into its own
block):
  * "settle per block" = call /settle repeatedly; each call accrues exactly 1
    block.
  * "batch over N blocks" = mine N-1 empty blocks, then one /settle (its own
    block is the N-th).
"""
from app.reference import RefAccount, SCALE

MAX_UINT256 = (1 << 256) - 1


# ---------- helpers ----------

def open_account(client, addr, principal, rate):
    r = client.post("/accounts", json={"account": addr, "principal": principal, "rate": rate})
    assert r.status_code == 200, r.text
    return r.json()


def get_state(client, addr):
    r = client.get(f"/accounts/{addr}")
    assert r.status_code == 200, r.text
    d = r.json()
    return {
        "principal": int(d["principal"]),
        "rate": int(d["rate"]),
        "last_settle_block": int(d["last_settle_block"]),
        "carry": int(d["carry"]),
        "fees": int(d["fees"]),
    }


def settle(client, addr):
    r = client.post(f"/accounts/{addr}/settle")
    assert r.status_code == 200, r.text
    return r.json()


def set_rate(client, addr, rate):
    r = client.post(f"/accounts/{addr}/rate", json={"rate": rate})
    assert r.status_code == 200, r.text
    return r.json()


def set_principal(client, addr, principal):
    r = client.post(f"/accounts/{addr}/principal", json={"principal": principal})
    assert r.status_code == 200, r.text
    return r.json()


def mine(client, n):
    r = client.post("/debug/mine", json={"blocks": n})
    assert r.status_code == 200, r.text
    return r.json()


# ---------- tests ----------

def test_health_and_scale(chain_env):
    client = chain_env["client"]
    h = client.get("/health")
    assert h.status_code == 200
    assert h.json()["status"] == "ok"
    s = client.get("/scale").json()
    assert int(s["rate_scale"]) == SCALE
    assert int(s["max_rate"]) == SCALE


def test_batch_vs_perblock_100_blocks(chain_env, fresh_account):
    """Core acceptance test: one settlement over 100 blocks == 100 one-block
    settlements == Python big-integer reference, exactly."""
    client = chain_env["client"]
    p, r, n = 10**24, SCALE // 100, 100

    acct_batch = fresh_account()
    open_account(client, acct_batch, p, r)
    mine(client, n - 1)
    settle(client, acct_batch)

    acct_per = fresh_account()
    open_account(client, acct_per, p, r)
    for _ in range(n):
        settle(client, acct_per)  # each tx is its own block -> 1 block each

    sb = get_state(client, acct_batch)
    sp = get_state(client, acct_per)

    ref = RefAccount(p, r, 0)
    ref.settle(n)

    assert sb["fees"] == sp["fees"] == ref.fees
    assert sb["carry"] == sp["carry"] == ref.carry
    # exact conservation: fees*SCALE + carry == n*p*r
    assert sb["fees"] * SCALE + sb["carry"] == n * p * r


def test_zero_principal(chain_env, fresh_account):
    client = chain_env["client"]
    acct = fresh_account()
    r = SCALE // 2
    open_account(client, acct, 0, r)
    mine(client, 9)
    resp = settle(client, acct)
    st = get_state(client, acct)
    assert st["fees"] == 0 and st["carry"] == 0
    assert resp["fee_delta"] == "0"

    # set a principal: only blocks after the change accrue
    sp = set_principal(client, acct, 10**18)
    mine(client, 9)
    resp2 = settle(client, acct)
    st2 = get_state(client, acct)

    ref = RefAccount(0, r, 0)
    ref.settle(10)  # 10 zero-principal blocks (9 mined + settle tx block)
    ref.set_principal(10**18, at_block=10)
    n2 = resp2["to_block"] - sp["block"]
    ref.settle(10 + n2)
    assert st2["fees"] == ref.fees and st2["carry"] == ref.carry
    assert st2["fees"] > 0


def test_tiny_rate_dust_not_lost(chain_env, fresh_account):
    """p=1, r=1: every block adds 1/2**64 of a unit. 100 blocks must still
    produce zero payable fee but a carry of exactly 100 — dust is carried,
    never burned."""
    client = chain_env["client"]
    acct = fresh_account()
    open_account(client, acct, 1, 1)
    mine(client, 99)
    settle(client, acct)
    st = get_state(client, acct)
    assert st["fees"] == 0
    assert st["carry"] == 100  # 100 blocks * 1 * 1, all below scale, all kept


def test_max_values(chain_env, fresh_account):
    """Maximum principal and maximum rate: fees exceed 256 bits, exercising
    the U512 path; result must match the Python big-integer reference."""
    client = chain_env["client"]
    acct = fresh_account()
    p, r, n = MAX_UINT256, SCALE, 100
    open_account(client, acct, p, r)
    mine(client, n - 1)
    settle(client, acct)
    st = get_state(client, acct)

    ref = RefAccount(p, r, 0)
    ref.settle(n)
    assert st["fees"] == ref.fees == n * p  # rate = 100% per block
    assert st["carry"] == 0
    assert st["fees"] > MAX_UINT256  # genuinely multi-limb (U512) result


def test_rounding_error_bound(chain_env, fresh_account):
    """For awkward p/r the per-block fee is heavily fractional; the settled
    amount must satisfy 0 <= exact - settled < 1 base unit, exactly."""
    client = chain_env["client"]
    acct = fresh_account()
    p, r, n = 3, SCALE // 7 + 1, 100
    open_account(client, acct, p, r)
    mine(client, n - 1)
    settle(client, acct)
    st = get_state(client, acct)

    exact_num = n * p * r  # exact fee = exact_num / SCALE
    q, rem = divmod(exact_num, SCALE)
    assert st["fees"] == q
    assert st["carry"] == rem
    assert 0 <= exact_num - st["fees"] * SCALE < SCALE


def test_no_double_counting(chain_env, fresh_account):
    """Repeated settlement only ever accrues newly elapsed blocks: the sum of
    all reported fee deltas equals the final balance equals the reference."""
    client = chain_env["client"]
    acct = fresh_account()
    p, r = 7 * 10**18, SCALE // 3
    open_account(client, acct, p, r)

    total_delta = 0
    blocks = 0
    for _ in range(5):  # 5 settles, 1 block each
        resp = settle(client, acct)
        total_delta += int(resp["fee_delta"])
        blocks += 1
    mine(client, 10)
    resp = settle(client, acct)  # +11 blocks
    total_delta += int(resp["fee_delta"])
    blocks += 11
    resp = settle(client, acct)  # +1 block (its own)
    total_delta += int(resp["fee_delta"])
    blocks += 1

    st = get_state(client, acct)
    ref = RefAccount(p, r, 0)
    ref.settle(blocks)
    assert st["fees"] == total_delta == ref.fees
    assert st["carry"] == ref.carry


def test_rate_change_settles_first(chain_env, fresh_account):
    """Changing the rate settles with the old rate up to the change block and
    applies the new rate only afterwards; matches segmented reference."""
    client = chain_env["client"]
    acct = fresh_account()
    p, r1, r2 = 5 * 10**21, SCALE // 50, SCALE // 200
    open_account(client, acct, p, r1)
    h0 = get_state(client, acct)["last_settle_block"]

    mine(client, 49)
    s1 = settle(client, acct)  # 50 blocks at r1
    assert s1["to_block"] - h0 == 50

    sr = set_rate(client, acct, r2)  # settles block h0+51 at r1
    assert sr["to_block"] - h0 == 51

    mine(client, 49)
    s2 = settle(client, acct)  # 50 blocks at r2
    assert s2["to_block"] - sr["to_block"] == 50

    ref = RefAccount(p, r1, h0)
    ref.settle(s1["to_block"])
    ref.set_rate(r2, at_block=sr["to_block"])
    ref.settle(s2["to_block"])

    st = get_state(client, acct)
    assert st["rate"] == r2
    assert st["fees"] == ref.fees and st["carry"] == ref.carry


def test_validation_errors(chain_env, fresh_account):
    client = chain_env["client"]
    acct = fresh_account()

    # rate above the cap rejected by the API
    r = client.post("/accounts", json={"account": acct, "principal": 1, "rate": SCALE + 1})
    assert r.status_code == 422

    # unknown account -> 404
    assert client.get(f"/accounts/{acct}").status_code == 404
    assert client.post(f"/accounts/{acct}/settle").status_code == 404

    # valid open, then duplicate open -> 400 (contract revert)
    open_account(client, acct, 100, 10)
    r = client.post("/accounts", json={"account": acct, "principal": 1, "rate": 1})
    assert r.status_code == 400

    # bad address -> 422
    r = client.post("/accounts", json={"account": "0x1234", "principal": 1, "rate": 1})
    assert r.status_code == 422
