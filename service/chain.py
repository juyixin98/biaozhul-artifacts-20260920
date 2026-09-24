"""Thin web3.py wrapper: artifact loading, contract deployment, transactions,
and Anvil-only time manipulation.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from eth_account import Account
from web3 import Web3
from web3.contract import Contract
from web3.exceptions import ContractLogicError

from .config import ARTIFACTS, ROOT


class ChainError(RuntimeError):
    """Raised for failed transactions with the on-chain revert reason attached."""

    def __init__(self, message: str, reason: str | None = None):
        super().__init__(message)
        self.reason = reason


def load_artifact(name: str, root: Path = ROOT) -> dict[str, Any]:
    path = root / ARTIFACTS[name]
    with open(path, "r", encoding="utf-8") as fh:
        artifact = json.load(fh)
    return {"abi": artifact["abi"], "bytecode": artifact["bytecode"]["object"]}


class ChainClient:
    def __init__(self, rpc_url: str, chain_id: int | None = None):
        self.w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 30}))
        if not self.w3.is_connected():
            raise ChainError(f"cannot connect to chain at {rpc_url}")
        self.rpc_url = rpc_url
        self.chain_id = chain_id or self.w3.eth.chain_id

    # ---- contracts --------------------------------------------------------

    def contract(self, name: str, address: str | None = None) -> Contract:
        artifact = load_artifact(name)
        return self.w3.eth.contract(
            address=Web3.to_checksum_address(address) if address else None,
            abi=artifact["abi"],
            bytecode=artifact["bytecode"],
        )

    def deploy(self, name: str, args: list[Any], private_key: str) -> tuple[str, dict]:
        contract = self.contract(name)
        txn = contract.constructor(*args)
        receipt = self.transact(txn, private_key)
        address = receipt["contractAddress"]
        return address, receipt

    # ---- transactions -----------------------------------------------------

    def transact(self, fn, private_key: str, value: int = 0) -> dict:
        """Build, sign, send and wait for a contract call.

        A target call that reverts *inside* the timelock's try/catch still
        produces a successful outer transaction (the failure is recorded on
        the op). Any revert that propagates out is raised as ChainError with
        the decoded reason.
        """
        account = Account.from_key(private_key)
        try:
            # No explicit gas: build_transaction estimates it, which surfaces
            # reverts (with their custom-error payload) before broadcasting.
            tx = fn.build_transaction(
                {
                    "from": account.address,
                    "nonce": self.w3.eth.get_transaction_count(account.address),
                    "gasPrice": self.w3.eth.gas_price,
                    "chainId": self.chain_id,
                    "value": value,
                }
            )
            signed = account.sign_transaction(tx)
            tx_hash = self.w3.eth.send_raw_transaction(signed.rawTransaction)
            receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash)
        except ContractLogicError as exc:
            # Custom errors surface as ContractCustomError with the raw revert
            # payload in .data (0x<selector><args>); string reverts carry the
            # reason in the message.
            data = getattr(exc, "data", None)
            if isinstance(data, dict):
                data = data.get("message") or str(data)
            reason = data if isinstance(data, str) and data else self._extract_reason(exc)
            raise ChainError(
                f"transaction reverted: {reason or self._short(exc)}",
                reason=reason,
            ) from exc

        if receipt["status"] != 1:
            raise ChainError(f"transaction failed: {receipt['transactionHash'].hex()}")
        return dict(receipt)

    # ---- Anvil helpers (dev only) ----------------------------------------

    def increase_time(self, seconds: int) -> int:
        # evm_increaseTime returns the total adjustment; evm_mine commits it.
        self.w3.provider.make_request("evm_increaseTime", [int(seconds)])
        self.w3.provider.make_request("evm_mine", [])
        return self.w3.eth.get_block("latest")["timestamp"]

    def now(self) -> int:
        return self.w3.eth.get_block("latest")["timestamp"]

    # ---- internals --------------------------------------------------------

    @staticmethod
    def _short(exc: Exception) -> str:
        text = str(exc)
        return text if len(text) <= 300 else text[:300] + "..."

    def _extract_reason(self, exc: ContractLogicError) -> str | None:
        message = str(exc)
        if not message or message.startswith("0x"):
            return None
        # web3 formats reverts as "execution reverted: <reason>" or just the
        # custom error name; keep the useful tail.
        for prefix in ("execution reverted: ", "execution reverted"):
            if message.startswith(prefix):
                return message[len(prefix):].strip() or None
        return message
