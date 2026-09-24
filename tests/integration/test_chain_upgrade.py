"""On-chain integration test: deploy V1 behind an ERC1967 proxy on local
Anvil, seed every storage type, upgrade to V2 (append-only, compatible) and
assert every datum survives. Also verifies the checker blocks V3/V4/V5
upgrades before any chain call.

Requires a running Anvil at http://127.0.0.1:8545 (see scripts/demo.sh).
"""
from __future__ import annotations

import pytest
from web3 import Web3

from checker import check_layouts
from checker.chain import (
    ANVIL_KEY_0,
    deploy_proxy_with,
    get_web3,
    load_bundle,
    read_demo_data,
    read_implementation,
    seed_demo_data,
    upgrade_proxy,
)


@pytest.fixture(scope="module")
def w3():
    try:
        return get_web3()
    except Exception as e:
        pytest.skip(f"anvil not running: {e}")


@pytest.fixture(scope="module")
def acct(w3):
    return w3.eth.account.from_key(ANVIL_KEY_0)


def test_upgrade_v1_to_v2_preserves_storage(w3, acct):
    v1 = load_bundle("BoxV1")
    v2 = load_bundle("BoxV2")

    # 1. checker must approve the pair before we touch the chain
    check = check_layouts(v1, v2)
    assert check.compatible is True

    # 2. deploy V1 + proxy, seed all storage types
    out = deploy_proxy_with(w3, acct, "BoxV1")
    proxy = out["proxy"]
    assert read_implementation(w3, proxy) == out["implementation"]
    seed_demo_data(w3, acct, proxy, v1["abi"])
    before = read_demo_data(w3, proxy, v1["abi"])
    assert before["x"] == 123456789
    assert before["name"] == "hello-upgrade"
    assert before["counts"] == [77, 88]
    assert before["values[5]"] == 555
    assert before["baseCounter"] == 42

    # 3. upgrade to V2
    up = upgrade_proxy(w3, acct, proxy, "BoxV2", v1["abi"])
    assert read_implementation(w3, proxy) == up["new_implementation"]

    # 4. every datum must survive the upgrade
    after = read_demo_data(w3, proxy, v2["abi"])
    for k in ("x", "packed_y_z_w", "name", "flags", "counts", "values[5]", "baseCounter"):
        assert after[k] == before[k], f"{k} changed across upgrade: {before[k]} -> {after[k]}"

    # 5. new V2 storage works and does not clobber V1 data
    c = w3.eth.contract(address=Web3.to_checksum_address(proxy), abi=v2["abi"])
    tx = c.functions.setExtra(999).build_transaction(
        {"from": acct.address, "nonce": w3.eth.get_transaction_count(acct.address),
         "gas": 200_000, "maxFeePerGas": w3.to_wei(20, "gwei"),
         "maxPriorityFeePerGas": w3.to_wei(1, "gwei"), "chainId": w3.eth.chain_id}
    )
    h = w3.eth.send_raw_transaction(acct.sign_transaction(tx).raw_transaction)
    assert w3.eth.wait_for_transaction_receipt(h).status == 1
    final = read_demo_data(w3, proxy, v2["abi"])
    assert final["extra"] == 999
    assert final["x"] == before["x"]  # still intact after writing new slots


def test_checker_blocks_incompatible_pairs():
    v1 = load_bundle("BoxV1")
    for bad in ("BoxV3", "BoxV4", "BoxV5"):
        r = check_layouts(v1, load_bundle(bad))
        assert r.compatible is False, f"{bad} should be blocked"
    assert check_layouts(v1, load_bundle("BoxV6")).compatible is True
