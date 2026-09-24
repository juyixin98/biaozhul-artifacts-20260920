"""Thin web3.py wrapper for the local Anvil chain and the settlement contracts."""
from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from eth_account import Account
from eth_abi import decode as abi_decode
from web3 import Web3
from web3.exceptions import ContractLogicError
from web3.types import TxReceipt

from .abi import erc20_abi, settlement_abi


class ChainError(Exception):
    """A contract call reverted or otherwise failed."""


def _build_error_registry(abi: list[dict]) -> dict[str, dict]:
    """Map 4-byte selector -> {name, inputs} for every custom error in an ABI."""
    registry: dict[str, dict] = {}
    for item in abi:
        if item.get("type") != "error":
            continue
        sig = f"{item['name']}({','.join(i['type'] for i in item['inputs'])})"
        selector = Web3.keccak(text=sig)[:4].hex()
        registry[selector] = item
    # A string-revert Error(string) is not listed in a custom-error ABI.
    return registry


# Built once from the settlement ABI; covers all contract custom errors.
_ERROR_REGISTRY = _build_error_registry(settlement_abi())


@dataclass
class TxResult:
    tx_hash: str
    block_number: int
    gas_used: int
    status: int
    receipt: TxReceipt


class LocalChain:
    def __init__(self, rpc_url: str):
        self.w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 30}))
        if not self.w3.is_connected():
            raise ConnectionError(f"cannot reach Ethereum node at {rpc_url}")
        self.chain_id = self.w3.eth.chain_id
        self._nonce_lock = threading.Lock()

    # ---- accounts ------------------------------------------------------

    def address_of(self, private_key: str) -> str:
        return Account.from_key(private_key).address

    def balance_eth(self, address: str) -> int:
        return self.w3.eth.get_balance(Web3.to_checksum_address(address))

    # ---- transactions --------------------------------------------------

    def send_tx(
        self,
        private_key: str,
        to: str | None,
        data: bytes | None = None,
        value: int = 0,
    ) -> TxResult:
        """Build, sign, send and wait for one transaction.

        Serializes nonce allocation per process so concurrent API requests
        cannot reuse a pending nonce.
        """
        acct = Account.from_key(private_key)
        with self._nonce_lock:
            nonce = self.w3.eth.get_transaction_count(acct.address, "pending")
            tx = {
                "from": acct.address,
                "nonce": nonce,
                "chainId": self.chain_id,
                "gas": 3_000_000,
                # anvil default base fee is 0; set a price anyway for realism
                "maxFeePerGas": self.w3.to_wei(20, "gwei"),
                "maxPriorityFeePerGas": self.w3.to_wei(1, "gwei"),
                "value": value,
            }
            if to is not None:
                tx["to"] = Web3.to_checksum_address(to)
            if data is not None:
                tx["data"] = data
            signed = acct.sign_transaction(tx)
            tx_hash = self.w3.eth.send_raw_transaction(signed.rawTransaction)
        receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)
        if receipt["status"] != 1:
            # The tx mined as reverted (e.g. a one-second timestamp race where
            # eth_call passed but the next block's timestamp didn't). Replay the
            # call to recover the revert data and decode it.
            reason = self._recover_revert(tx)
            raise ChainError(reason or f"transaction {tx_hash.hex()} reverted (status 0)")
        return TxResult(
            tx_hash=tx_hash.hex(),
            block_number=receipt["blockNumber"],
            gas_used=receipt["gasUsed"],
            status=receipt["status"],
            receipt=receipt,
        )

    # ---- contract bindings ---------------------------------------------

    def settlement(self, address: str):
        return self.w3.eth.contract(
            address=Web3.to_checksum_address(address), abi=settlement_abi()
        )

    def erc20(self, address: str):
        return self.w3.eth.contract(
            address=Web3.to_checksum_address(address), abi=erc20_abi()
        )

    # ---- token helpers -------------------------------------------------

    def token_balance(self, token: str, holder: str) -> int:
        return self.erc20(token).functions.balanceOf(
            Web3.to_checksum_address(holder)
        ).call()

    def token_approve(self, token: str, private_key: str, spender: str, amount: int) -> TxResult:
        spender_cs = Web3.to_checksum_address(spender)
        contract = self.erc20(token)
        data = contract.encode_abi("approve", args=[spender_cs, amount])
        return self.send_tx(private_key, to=contract.address, data=data)

    # ---- settlement helpers --------------------------------------------

    def simulate_fill(self, settlement_addr: str, order_tuple: tuple,
                      spent: int, fee: int, signature: str, taker: str) -> int:
        """Dry-run fillOrder and return takerDue; raises ChainError on revert."""
        c = self.settlement(settlement_addr)
        try:
            return c.functions.fillOrder(order_tuple, spent, fee, bytes.fromhex(signature.removeprefix("0x"))).call(
                {"from": Web3.to_checksum_address(taker)}
            )
        except ContractLogicError as exc:
            raise ChainError(self._decode_revert(exc)) from exc

    def fill_order(self, settlement_addr: str, taker_key: str, order_tuple: tuple,
                   spent: int, fee: int, signature: str) -> tuple[TxResult, int]:
        c = self.settlement(settlement_addr)
        taker = self.address_of(taker_key)
        taker_due = self.simulate_fill(settlement_addr, order_tuple, spent, fee, signature, taker)
        data = c.encode_abi(
            "fillOrder",
            args=[order_tuple, spent, fee, bytes.fromhex(signature.removeprefix("0x"))],
        )
        tx = self.send_tx(taker_key, to=c.address, data=data)
        return tx, taker_due

    def cancel_nonce(self, settlement_addr: str, maker_key: str, nonce: int) -> TxResult:
        c = self.settlement(settlement_addr)
        data = c.encode_abi("cancelNonce", args=[nonce])
        return self.send_tx(maker_key, to=c.address, data=data)

    def fill_status(self, settlement_addr: str, order_hash_hex: str) -> dict[str, int]:
        c = self.settlement(settlement_addr)
        h = bytes.fromhex(order_hash_hex.removeprefix("0x"))
        return {
            "filledMakerAmount": c.functions.filledMakerAmount(h).call(),
            "cumulativeFee": c.functions.cumulativeFee(h).call(),
            "filledTakerAmount": c.functions.filledTakerAmount(h).call(),
        }

    def nonce_cancelled(self, settlement_addr: str, maker: str, nonce: int) -> bool:
        return self.settlement(settlement_addr).functions.cancelledNonce(
            Web3.to_checksum_address(maker), nonce
        ).call()

    # ---- revert recovery -----------------------------------------------

    def _recover_revert(self, tx: dict) -> str | None:
        """Replay a mined transaction as eth_call to fetch its revert payload."""
        call = {
            "from": tx["from"],
            "to": tx.get("to"),
            "data": tx.get("data", b""),
            "value": tx.get("value", 0),
        }
        try:
            self.w3.eth.call(call)
            return None
        except ContractLogicError as exc:
            return self._decode_revert(exc)
        except Exception:  # noqa: BLE001 - best effort only
            return None

    @staticmethod
    def _decode_revert(exc: ContractLogicError) -> str:
        # web3.py 6 does not decode custom errors; the revert payload may appear
        # as exc.data, in exc.args, or as a trailing hex token in the text.
        raw = getattr(exc, "data", None)
        if isinstance(raw, dict):  # newer web3 shapes: {"data": "0x.."}
            raw = raw.get("data") or raw.get("original", "")
        if not raw or not isinstance(raw, str) or not raw.startswith("0x"):
            for arg in getattr(exc, "args", ()):
                if isinstance(arg, str) and arg.startswith("0x") and len(arg) >= 10:
                    raw = arg
                    break
        if not raw or not isinstance(raw, str) or len(raw) < 10:
            return str(exc)

        selector = raw[:10]
        body = bytes.fromhex(raw[10:])

        # Solidity require-style revert: Error(string) -> selector 0x08c379a0
        if selector == "0x08c379a0":
            try:
                (reason,) = abi_decode(["string"], body)
                return reason
            except Exception:  # noqa: BLE001
                return raw
        # Panic(uint256) -> 0x4e487b71
        if selector == "0x4e487b71":
            try:
                (code,) = abi_decode(["uint256"], body)
                return f"Panic(0x{code:x})"
            except Exception:  # noqa: BLE001
                return raw

        item = _ERROR_REGISTRY.get(selector)
        if item is None:
            return raw
        types = [i["type"] for i in item["inputs"]]
        names = [i["name"] for i in item["inputs"]]
        if not types:
            return item["name"]
        try:
            values = abi_decode(types, body)
        except Exception:  # noqa: BLE001
            return f"{item['name']}(<undecodable>)"
        rendered = ", ".join(f"{n}={v}" for n, v in zip(names, values))
        return f"{item['name']}({rendered})"
