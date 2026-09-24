"""Direct web3.py tests: conservation, concurrent cancel/fill race,
transfer-failure rollback and domain binding, all against live Anvil."""
from __future__ import annotations

import threading
import time

import pytest
from web3 import Web3
from web3.exceptions import ContractLogicError

from app.chain import ChainError, LocalChain
from app.signing import Order, sign_order

ZERO = "0x0000000000000000000000000000000000000000"


def _make_order(api, nonce: int, maker_amount=1_000_000_000, taker_amount=3,
                deadline=None, fee_recipient=ZERO, max_fee=0, taker_addr=ZERO) -> tuple[Order, str]:
    chain: LocalChain = api["chain"]
    dep = api["deployment"]
    maker = chain.address_of(api["keys"]["maker"])
    order = Order(
        maker=maker,
        taker=Web3.to_checksum_address(taker_addr),
        makerToken=Web3.to_checksum_address(dep["tokenA"]),
        takerToken=Web3.to_checksum_address(dep["tokenB"]),
        makerAmount=maker_amount,
        takerAmount=taker_amount,
        nonce=nonce,
        deadline=deadline or int(time.time()) + 3600,
        feeRecipient=Web3.to_checksum_address(fee_recipient),
        maxFeeAmount=max_fee,
    )
    sig = sign_order(api["keys"]["maker"], chain.chain_id, dep["settlement"], order)
    return order, sig


def _balances(api, *holders):
    chain: LocalChain = api["chain"]
    dep = api["deployment"]
    return [
        (chain.token_balance(dep["tokenA"], h), chain.token_balance(dep["tokenB"], h))
        for h in holders
    ]


# --------------------------------------------------------------------- //
# two partial fills: rounding + conservation                            //
# --------------------------------------------------------------------- //


def test_two_partial_fills_rounding_and_conservation(api):
    chain: LocalChain = api["chain"]
    dep = api["deployment"]
    taker = chain.address_of(api["keys"]["taker"])
    maker = chain.address_of(api["keys"]["maker"])
    fee = chain.address_of(api["keys"]["feeRecipient"])

    order, sig = _make_order(
        api, nonce=101, maker_amount=600, taker_amount=400,
        fee_recipient=fee, max_fee=60,
    )

    before = {
        "maker": _balances(api, maker)[0],
        "taker": _balances(api, taker)[0],
        "fee": _balances(api, fee)[0],
        # supply of each mock token is constant: sum over all parties
    }

    # two partials (100 A gross, 10 fee each -> 90 proceeds), then 400/40.
    # Pro-rata B: ceil(90*400/600) = 60 per partial; final clears remainder.
    tx1, due1 = chain.fill_order(dep["settlement"], api["keys"]["taker"], order.to_tuple(), 100, 10, sig)
    tx2, due2 = chain.fill_order(dep["settlement"], api["keys"]["taker"], order.to_tuple(), 100, 10, sig)
    tx3, due3 = chain.fill_order(dep["settlement"], api["keys"]["taker"], order.to_tuple(), 400, 40, sig)

    assert due1 == 60
    assert due2 == 60
    assert due3 == 280  # final dust-clearing: 400 - 120
    assert due1 + due2 + due3 == 400

    after = {
        "maker": _balances(api, maker)[0],
        "taker": _balances(api, taker)[0],
        "fee": _balances(api, fee)[0],
    }

    # signed-amount bounds: maker gives exactly 600 gross (540 proceeds + 60 fee)
    assert before["maker"][0] - after["maker"][0] == 600
    assert after["taker"][0] - before["taker"][0] == 540
    assert after["fee"][0] - before["fee"][0] == 60

    # conservation of A: maker loss == taker gain + fee gain
    a_lost = before["maker"][0] - after["maker"][0]
    a_gained = (after["taker"][0] - before["taker"][0]) + (after["fee"][0] - before["fee"][0])
    assert a_lost == a_gained == 600

    # conservation of B: taker loss == maker gain, exactly signed amount
    b_lost = before["taker"][1] - after["taker"][1]
    b_gained = after["maker"][1] - before["maker"][1]
    assert b_lost == b_gained == 400


def test_volume_never_exceeds_signed_amount(api):
    chain: LocalChain = api["chain"]
    dep = api["deployment"]
    order, sig = _make_order(api, nonce=102, maker_amount=100, taker_amount=30)

    # 10 small fills of 10 each (sums to 100); an 11th must revert
    duess = []
    for _ in range(10):
        _, due = chain.fill_order(dep["settlement"], api["keys"]["taker"], order.to_tuple(), 10, 0, sig)
        duess.append(due)
    # first 9 pay ceil(3)=3; the final clears remainder 30-27 = 3 here (exact)
    assert sum(duess) == 30

    with pytest.raises(ChainError, match="FillExceedsOrder"):
        chain.fill_order(dep["settlement"], api["keys"]["taker"], order.to_tuple(), 10, 0, sig)


# --------------------------------------------------------------------- //
# cancellation races                                                    //
# --------------------------------------------------------------------- //


def test_cancel_and_fill_race_only_one_wins(api):
    """Fire cancel and fill concurrently; exactly one outcome is possible,
    and assets stay consistent whichever wins."""
    chain: LocalChain = api["chain"]
    dep = api["deployment"]
    maker = chain.address_of(api["keys"]["maker"])
    taker = chain.address_of(api["keys"]["taker"])

    outcomes = {"cancelled": None, "filled": None, "errors": []}

    def cancel():
        try:
            chain.cancel_nonce(dep["settlement"], api["keys"]["maker"], 201)
            outcomes["cancelled"] = True
        except Exception as exc:  # noqa: BLE001
            outcomes["errors"].append(("cancel", str(exc)))

    def fill():
        order, sig = _make_order(api, nonce=201, maker_amount=100, taker_amount=30)
        try:
            _, due = chain.fill_order(
                dep["settlement"], api["keys"]["taker"], order.to_tuple(), 100, 0, sig
            )
            outcomes["filled"] = due
        except ChainError as exc:
            # If cancel lands first the fill MUST revert NonceCancelled.
            outcomes["errors"].append(("fill", str(exc)))

    t1 = threading.Thread(target=cancel)
    t2 = threading.Thread(target=fill)
    t1.start(); t2.start()
    t1.join(10); t2.join(10)
    assert not t1.is_alive() and not t2.is_alive(), "race threads hung"

    # Exactly one of two terminal states: either the fill landed once
    # (cancellation arriving after changes nothing) or the cancellation landed
    # first and the fill reverted NonceCancelled with zero assets moved.
    from app.signing import order_hash
    order, sig = _make_order(api, nonce=201, maker_amount=100, taker_amount=30)
    status = chain.fill_status(
        dep["settlement"], order_hash(chain.chain_id, dep["settlement"], order)
    )
    if outcomes["filled"] is not None:
        assert outcomes["filled"] == 30
        assert status["filledMakerAmount"] == 100
        assert status["filledTakerAmount"] == 30
        assert all(e[0] != "fill" for e in outcomes["errors"])
    else:
        assert any("NonceCancelled" in e[1] for e in outcomes["errors"])
        assert status["filledMakerAmount"] == 0
        assert status["filledTakerAmount"] == 0


def test_cancelled_nonce_blocks_new_order_with_same_nonce(api):
    chain: LocalChain = api["chain"]
    dep = api["deployment"]
    chain.cancel_nonce(dep["settlement"], api["keys"]["maker"], 202)
    order, sig = _make_order(api, nonce=202, maker_amount=100, taker_amount=30)
    with pytest.raises(ChainError, match="NonceCancelled"):
        chain.fill_order(dep["settlement"], api["keys"]["taker"], order.to_tuple(), 100, 0, sig)


# --------------------------------------------------------------------- //
# replay protection                                                     //
# --------------------------------------------------------------------- //


def test_replay_same_signature_after_full_fill(api):
    chain: LocalChain = api["chain"]
    dep = api["deployment"]
    order, sig = _make_order(api, nonce=301, maker_amount=100, taker_amount=30)
    chain.fill_order(dep["settlement"], api["keys"]["taker"], order.to_tuple(), 100, 0, sig)
    with pytest.raises(ChainError, match="FillExceedsOrder"):
        chain.fill_order(dep["settlement"], api["keys"]["taker"], order.to_tuple(), 1, 0, sig)


# --------------------------------------------------------------------- //
# transfer failure => whole fill rolls back                             //
# --------------------------------------------------------------------- //


def test_failed_transfer_rolls_back_entire_fill(api):
    """Taker receives maker tokens first; if the taker's B payment fails,
    the maker->taker A transfer and fill bookkeeping MUST both revert."""
    chain: LocalChain = api["chain"]
    dep = api["deployment"]
    w3 = api["w3"]

    # Fund a FRESH EOA with B tokens but never approve the settlement.
    fresh_key = "0x" + "11" * 32
    fresh = chain.address_of(fresh_key)
    # give it ETH for gas from deployer and B tokens from the minter (deployer)
    chain.send_tx(api["keys"]["deployer"], to=fresh, value=w3.to_wei(1, "ether"))
    b = chain.erc20(dep["tokenB"])
    chain.send_tx(
        api["keys"]["deployer"], to=dep["tokenB"],
        data=b.encode_abi("mint", args=[fresh, 1_000_000]),
    )

    order, sig = _make_order(api, nonce=401, maker_amount=100, taker_amount=30)
    maker = order.maker

    a_before_maker = chain.token_balance(dep["tokenA"], maker)
    a_before_fresh = chain.token_balance(dep["tokenA"], fresh)

    with pytest.raises(ChainError, match="insufficient allowance"):
        chain.fill_order(dep["settlement"], fresh_key, order.to_tuple(), 100, 0, sig)

    # no maker token leaked to the failing taker ...
    assert chain.token_balance(dep["tokenA"], maker) == a_before_maker
    assert chain.token_balance(dep["tokenA"], fresh) == a_before_fresh
    # ... and no fill was recorded
    from app.signing import order_hash
    status = chain.fill_status(
        dep["settlement"], order_hash(chain.chain_id, dep["settlement"], order)
    )
    assert status["filledMakerAmount"] == 0


# --------------------------------------------------------------------- //
# signature domain binds chain id + contract                            //
# --------------------------------------------------------------------- //


def test_signature_invalid_on_second_deployment(api, anvil):
    """A signature for deployment 1 must not validate against an independent
    settlement deployment on the same chain (different verifyingContract)."""
    import os
    import subprocess
    import sys
    from pathlib import Path

    chain: LocalChain = api["chain"]

    env = os.environ.copy()
    env["RPC_URL"] = anvil
    env["CHAIN_ID"] = "31337"
    dep2_file = Path(api["deployment"]["_file"]).parent / "deployment2.json"
    env["DEPLOYMENT_FILE"] = str(dep2_file)
    result = subprocess.run(
        [sys.executable, "-m", "scripts.deploy"],
        cwd=Path(__file__).resolve().parent.parent,
        env=env, capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, result.stderr

    import json
    dep2 = json.loads(dep2_file.read_text())
    assert dep2["settlement"].lower() != api["deployment"]["settlement"].lower()

    # Sign against deployment 1, try to fill on deployment 2.
    order1, sig1 = _make_order(api, nonce=501, maker_amount=100, taker_amount=30)
    c2 = chain.settlement(dep2["settlement"])
    # deploy2 minted tokens to the same maker/taker addresses and approved c1;
    # approve c2 too so signature is the ONLY thing checked first.
    chain.token_approve(dep2["tokenA"], api["keys"]["maker"], dep2["settlement"], 2**256 - 1)
    chain.token_approve(dep2["tokenB"], api["keys"]["taker"], dep2["settlement"], 2**256 - 1)

    with pytest.raises(ContractLogicError):
        c2.functions.fillOrder(
            order1.to_tuple(), 100, 0, bytes.fromhex(sig1.removeprefix("0x"))
        ).call({"from": chain.address_of(api["keys"]["taker"])})


def test_simulate_revert_messages_are_readable(api):
    chain: LocalChain = api["chain"]
    dep = api["deployment"]
    order, sig = _make_order(api, nonce=601, maker_amount=100, taker_amount=30,
                             deadline=int(time.time()) - 10)
    with pytest.raises(ChainError, match="OrderExpired"):
        chain.fill_order(dep["settlement"], api["keys"]["taker"], order.to_tuple(), 100, 0, sig)
