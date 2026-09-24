"""Contract artifact loading, deployment and a typed HTLC client.

One contract (src/HashedTimelock.sol) is deployed independently to each local
chain. There is deliberately no bridge/messaging: the Python layer only reads
both legs and reports their states.
"""

from __future__ import annotations

import json
import secrets as _secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from eth_utils import keccak
from web3 import Web3
from web3.contract import Contract
from web3.types import TxReceipt

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ARTIFACT = PROJECT_ROOT / "out" / "HashedTimelock.sol" / "HashedTimelock.json"

STATE_NAMES = ("NONEXISTENT", "LOCKED", "CLAIMED", "REFUNDED")


def load_artifact() -> dict[str, Any]:
    if not ARTIFACT.exists():
        raise FileNotFoundError(f"missing {ARTIFACT}; run `forge build` first")
    return json.loads(ARTIFACT.read_text())


def get_contract(w3: Web3, address: str) -> Contract:
    artifact = load_artifact()
    return w3.eth.contract(address=Web3.to_checksum_address(address),
                           abi=artifact["abi"])


def deploy(w3: Web3, from_addr: str, private_key: str) -> tuple[Contract, TxReceipt]:
    artifact = load_artifact()
    contract = w3.eth.contract(abi=artifact["abi"],
                               bytecode=artifact["bytecode"]["object"])
    sender = Web3.to_checksum_address(from_addr)
    tx = contract.constructor().build_transaction({
        "from": sender,
        "nonce": w3.eth.get_transaction_count(sender),
        "gas": 3_000_000,
        "gasPrice": w3.eth.gas_price,
        "chainId": w3.eth.chain_id,
    })
    signed = w3.eth.account.sign_transaction(tx, private_key=private_key)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash)
    if receipt["status"] != 1:
        raise RuntimeError("deployment reverted")
    return get_contract(w3, receipt["contractAddress"]), receipt


def new_secret() -> tuple[bytes, bytes]:
    """Return (preimage, hashLock=keccak256(preimage)); preimage is 32 bytes."""
    preimage = _secrets.token_bytes(32)
    return preimage, keccak(preimage)


def hash_of(preimage: bytes) -> bytes:
    return keccak(preimage)


def _addr20(addr: str) -> bytes:
    return bytes.fromhex(Web3.to_checksum_address(addr)[2:])


def derive_swap_id(hash_lock: bytes, sender: str, receiver: str,
                   amount: int, timelock: int) -> bytes:
    """Mirror on-chain keccak256(abi.encodePacked(bytes32,addr,addr,uint256,uint64)).

    Packed widths: 32 + 20 + 20 + 32 + 8 = 112 bytes. The authoritative swapId
    is the one emitted in Locked; this helper is only a cross-check.
    """
    packed = (
        hash_lock
        + _addr20(sender)
        + _addr20(receiver)
        + int(amount).to_bytes(32, "big")
        + int(timelock).to_bytes(8, "big")
    )
    return keccak(packed)


@dataclass
class SwapView:
    swap_id: str
    state: str
    state_code: int
    hash_lock: str
    sender: str
    receiver: str
    amount_wei: int
    amount_eth: float
    timelock: int
    now: int
    seconds_remaining: int | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "swap_id": self.swap_id,
            "state": self.state,
            "state_code": self.state_code,
            "hash_lock": self.hash_lock,
            "sender": self.sender,
            "receiver": self.receiver,
            "amount_wei": str(self.amount_wei),
            "amount_eth": self.amount_eth,
            "timelock": self.timelock,
            "now": self.now,
            "seconds_remaining": self.seconds_remaining,
        }


class HtlcClient:
    """Signing wrapper around one deployed HashedTimelock on one chain."""

    def __init__(self, w3: Web3, address: str):
        self.w3 = w3
        self.contract = get_contract(w3, address)
        self.address = Web3.to_checksum_address(address)

    def _send(self, func: Any, sender: str, private_key: str,
              value: int = 0) -> TxReceipt:
        sender = Web3.to_checksum_address(sender)
        # Pre-execute so contract reverts surface as exceptions instead of a
        # silently mined status=0 receipt.
        func.call({"from": sender, "value": value})
        tx = func.build_transaction({
            "from": sender,
            "nonce": self.w3.eth.get_transaction_count(sender),
            "gas": 500_000,
            "gasPrice": self.w3.eth.gas_price,
            "chainId": self.w3.eth.chain_id,
            "value": value,
        })
        signed = self.w3.eth.account.sign_transaction(tx, private_key=private_key)
        tx_hash = self.w3.eth.send_raw_transaction(signed.raw_transaction)
        receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash)
        if receipt["status"] != 1:
            raise RuntimeError("transaction mined with status=0")
        return receipt

    def lock(self, hash_lock: bytes, sender: str, sender_pk: str,
             receiver: str, amount_wei: int, timelock: int) -> tuple[bytes, TxReceipt]:
        receiver = Web3.to_checksum_address(receiver)
        receipt = self._send(
            self.contract.functions.lock(hash_lock, receiver, int(timelock)),
            sender, sender_pk, value=amount_wei)
        logs = self.contract.events.Locked().process_receipt(receipt)
        swap_id = bytes(logs[0]["args"]["swapId"])
        return swap_id, receipt

    def claim(self, swap_id: bytes, preimage: bytes,
              sender: str, sender_pk: str, wait: bool = True) -> Any:
        tx_hash = None
        cs = Web3.to_checksum_address(sender)
        func = self.contract.functions.claim(swap_id, preimage)
        if wait:
            func.call({"from": cs})  # surface reverts as exceptions
        tx = func.build_transaction({
            "from": cs,
            "nonce": self.w3.eth.get_transaction_count(cs),
            "gas": 500_000,
            "gasPrice": self.w3.eth.gas_price,
            "chainId": self.w3.eth.chain_id,
        })
        signed = self.w3.eth.account.sign_transaction(tx, private_key=sender_pk)
        tx_hash = self.w3.eth.send_raw_transaction(signed.raw_transaction)
        if wait:
            receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash)
            if receipt["status"] != 1:
                raise RuntimeError("claim mined with status=0")
            return receipt
        return tx_hash.hex()

    def refund(self, swap_id: bytes, sender: str, sender_pk: str) -> TxReceipt:
        return self._send(self.contract.functions.refund(swap_id), sender, sender_pk)

    def raw_swap(self, swap_id: bytes) -> tuple:
        return self.contract.functions.swaps(swap_id).call()

    def state_code(self, swap_id: bytes) -> int:
        return int(self.contract.functions.getState(swap_id).call())

    def view_swap(self, swap_id: bytes) -> SwapView:
        code = self.state_code(swap_id)
        now_ts = self.w3.eth.get_block("latest")["timestamp"]
        if code == 0:
            return SwapView(
                swap_id=swap_id.hex(), state="NONEXISTENT", state_code=0,
                hash_lock="0x" + "00" * 32, sender="", receiver="",
                amount_wei=0, amount_eth=0.0, timelock=0, now=now_ts,
                seconds_remaining=None)
        hash_lock, snd, rcv, amount, timelock, _st = self.raw_swap(swap_id)
        return SwapView(
            swap_id=swap_id.hex(),
            state=STATE_NAMES[code],
            state_code=code,
            hash_lock=hash_lock.hex(),
            sender=snd,
            receiver=rcv,
            amount_wei=int(amount),
            amount_eth=float(Web3.from_wei(amount, "ether")),
            timelock=int(timelock),
            now=int(now_ts),
            seconds_remaining=max(0, int(timelock) - int(now_ts)),
        )

    def balance(self) -> int:
        return self.w3.eth.get_balance(self.address)
