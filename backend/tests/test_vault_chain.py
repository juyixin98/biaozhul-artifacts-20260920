"""链上整数会计测试（经 web3.py 直连本机 Anvil）。"""
from __future__ import annotations

import pytest

from backend.app import chain as chainlib
from backend.app.config import ANVIL_TEST_PRIVATE_KEYS

WAD = 10**18
DEAD = "0x0000000000000000000000000000000000000001"


def _send(bundle, acct, func):
    return chainlib._send(bundle.w3, acct, func)


def _mint_and_approve(bundle, acct, amount):
    _send(bundle, acct, bundle.token.functions.mint(amount))
    _send(bundle, acct, bundle.token.functions.approve(bundle.vault_address, 2**256 - 1))


def test_first_deploy_is_empty(bundle):
    assert bundle.vault.functions.totalSupply().call() == 0
    assert bundle.vault.functions.totalAssets().call() == 0


def test_first_deposit_locks_dead_shares(bundle, accounts):
    alice = accounts[0]
    _mint_and_approve(bundle, alice, 5_000 * WAD)

    tx = _send(bundle, alice, bundle.vault.functions.deposit(5_000 * WAD, alice.address, 0))
    events = bundle.vault.events.Deposit().process_receipt(tx)
    shares = events[0]["args"]["shares"]

    assert shares == 5_000 * WAD - 1000
    assert bundle.vault.functions.balanceOf(DEAD).call() == 1000
    assert bundle.vault.functions.totalSupply().call() == 5_000 * WAD
    assert bundle.vault.functions.totalAssets().call() == 5_000 * WAD


def test_deposit_and_redeem_round_trip(bundle, accounts):
    alice = accounts[0]
    _mint_and_approve(bundle, alice, 10_000 * WAD)
    _send(bundle, alice, bundle.vault.functions.deposit(2_000 * WAD, alice.address, 0))

    shares = bundle.vault.functions.balanceOf(alice.address).call()
    expected = bundle.vault.functions.previewRedeem(shares).call()
    before = bundle.token.functions.balanceOf(alice.address).call()
    _send(bundle, alice, bundle.vault.functions.redeem(shares, alice.address, alice.address, 0))
    after = bundle.token.functions.balanceOf(alice.address).call()

    # 首存 1:1，只剩死份额损失（1000），没有额外误差。
    assert after - before == expected
    assert (after - before) == 2_000 * WAD - 1000
    assert bundle.vault.functions.totalSupply().call() == 1000


def test_tiny_deposit_reverts_zero_shares(bundle, accounts):
    alice, bob = accounts[0], accounts[1]
    deposited = 10**24
    donation = 999 * 10**24
    _mint_and_approve(bundle, alice, deposited + donation)
    _send(bundle, alice, bundle.vault.functions.deposit(deposited, alice.address, 0))
    # 大额捐赠把汇率抬到 1000:1。
    _send(bundle, alice, bundle.token.functions.transfer(bundle.vault_address, donation))

    _mint_and_approve(bundle, bob, 999)
    with pytest.raises(chainlib.ChainError, match="ZeroShares|交易回滚"):
        _send(bundle, bob, bundle.vault.functions.deposit(999, bob.address, 0))
    assert bundle.vault.functions.balanceOf(bob.address).call() == 0
    assert bundle.token.functions.balanceOf(bob.address).call() == 999


def test_donation_to_empty_vault_mints_no_shares(bundle, accounts):
    alice, bob = accounts[0], accounts[1]
    _mint_and_approve(bundle, alice, 100 * WAD)
    _send(bundle, alice, bundle.token.functions.transfer(bundle.vault_address, 100 * WAD))
    assert bundle.vault.functions.totalSupply().call() == 0

    _mint_and_approve(bundle, bob, 1_000 * WAD)
    _send(bundle, bob, bundle.vault.functions.deposit(1_000 * WAD, bob.address, 0))
    assert bundle.vault.functions.balanceOf(bob.address).call() == 1_000 * WAD - 1000
    assert bundle.vault.functions.totalAssets().call() == 1_100 * WAD


def test_slippage_floor_integer_arithmetic():
    # floor(x * (10000 - bps) / 10000)
    assert chainlib.apply_slippage_floor(100, 0) == 100
    assert chainlib.apply_slippage_floor(100, 50) == 99      # floor(100*9950/10000)=99
    assert chainlib.apply_slippage_floor(1, 1) == 0          # floor(1*9999/10000)=0
    assert chainlib.apply_slippage_floor(999, 100) == 989    # floor(999*9900/10000)=989
    assert chainlib.apply_slippage_floor(1234, 100) == 1221  # floor(1234*9900/10000)=1221
    with pytest.raises(ValueError):
        chainlib.apply_slippage_floor(100, 10_000)
