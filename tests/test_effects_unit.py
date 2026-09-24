"""Pure-function tests: forward/inverse effects and log topic0 hashes."""

from __future__ import annotations

from app.effects import apply_forward, apply_inverse
from app.chain import TOPIC_TO_EVENT

ALICE = "0xAA00000000000000000000000000000000000001"
BOB = "0xBB00000000000000000000000000000000000002"


def ev(name, amount, account=ALICE, to=None, tag=1):
    return {
        "name": name,
        "account": account,
        "to_account": to,
        "amount": amount,
        "tag": tag,
    }


def test_forward_then_inverse_returns_to_origin():
    sequence = [
        ev("Deposited", 100),
        ev("Transferred", 30, to=BOB),
        ev("Withdrawn", 5),
        ev("Deposited", 2, account=BOB),
    ]
    state: dict[str, int] = {}
    for e in sequence:
        apply_forward(state, e)
    assert state == {ALICE: 65, BOB: 32}

    for e in reversed(sequence):
        apply_inverse(state, e)
    assert state == {ALICE: 0, BOB: 0}


def test_topic0_matches_solidity_signatures():
    # topic0 == keccak256 of the canonical event signatures.
    from eth_utils import keccak

    expected = {
        "Deposited": "0x" + keccak(text="Deposited(address,uint256,uint256)").hex(),
        "Withdrawn": "0x" + keccak(text="Withdrawn(address,uint256,uint256)").hex(),
        "Transferred": "0x"
        + keccak(text="Transferred(address,address,uint256,uint256)").hex(),
    }
    assert len(TOPIC_TO_EVENT) == 3
    for topic, name in TOPIC_TO_EVENT.items():
        assert topic == expected[name]
