"""Contract ABI, deployment helper and typed on-chain wrapper."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from web3 import Web3
from web3.types import HexBytes, TxReceipt

_ABI_PATH = Path(__file__).resolve().parent.parent / "abi" / "Checkpoints.json"


def load_abi() -> list:
    """Minimal hand-maintained ABI for src/Checkpoints.sol."""
    return json.loads(_ABI_PATH.read_text())


@dataclass(frozen=True)
class LookupResult:
    exists: bool
    block_number: int  # block the returned checkpoint was written at
    value: int


@dataclass(frozen=True)
class LatestResult:
    exists: bool
    block_number: int
    value: int


class FutureBlockError(Exception):
    """Requested block is later than the chain's current block."""

    def __init__(self, requested: int, current: int):
        self.requested = requested
        self.current = current
        super().__init__(
            f"target block {requested} is in the future; "
            f"current block is {current}"
        )


# Selector of the contract's custom error: FutureBlock(uint256,uint256).
# Computed at import time from the signature; the 4-byte selector alone is
# enough to recognise the revert (the ABI-encoded args are (requested,current)).
_FUTURE_BLOCK_SELECTOR = bytes.fromhex("1e03d53c")


class CheckpointContract:
    """Thin, typed wrapper around the deployed Checkpoints contract."""

    GAS_PER_SET = 200_000

    def __init__(self, w3: Web3, address: str, private_key: str):
        self.w3 = w3
        self.address = Web3.to_checksum_address(address)
        self.contract = w3.eth.contract(address=self.address, abi=load_abi())
        self.account = w3.eth.account.from_key(private_key)

    # ---- reads ----

    def current_block(self) -> int:
        return int(self.w3.eth.block_number)

    def length(self) -> int:
        return int(self.contract.functions.length().call())

    def latest(self) -> LatestResult:
        exists, block_number, value = self.contract.functions.latest().call()
        return LatestResult(bool(exists), int(block_number), int(value))

    def get_at_block(self, target_block: int) -> LookupResult:
        try:
            exists, block_number, value = self.contract.functions.getAtBlock(
                int(target_block)
            ).call()
        except Exception as exc:  # web3 raises ContractLogicError on revert
            data = _revert_data(exc)
            if data is not None and data[:4] == _FUTURE_BLOCK_SELECTOR and len(data) >= 68:
                requested = int.from_bytes(data[4:36], "big")
                current = int.from_bytes(data[36:68], "big")
                raise FutureBlockError(requested, current) from exc
            raise
        return LookupResult(bool(exists), int(block_number), int(value))

    # ---- writes ----

    def set_value(self, value: int) -> TxReceipt:
        """Submit a setValue transaction and wait for inclusion."""
        if value < 0:
            raise ValueError("value must be a non-negative uint256")
        if value > 2**256 - 1:
            raise ValueError("value exceeds uint256")
        account = self.account.address
        tx = self.contract.functions.setValue(int(value)).build_transaction(
            {
                "from": account,
                "nonce": self.w3.eth.get_transaction_count(account),
                "gas": self.GAS_PER_SET,
                "gasPrice": self.w3.eth.gas_price,
                "chainId": int(self.w3.eth.chain_id),
            }
        )
        signed = self.w3.eth.account.sign_transaction(tx, self.account.key)
        tx_hash: HexBytes = self.w3.eth.send_raw_transaction(signed.raw_transaction)
        return self.w3.eth.wait_for_transaction_receipt(tx_hash)


def _revert_data(exc: Exception) -> bytes | None:
    """Extract ABI-encoded revert data from a web3 call exception."""
    # web3>=6 surfaces it in different places depending on the provider.
    data = getattr(exc, "data", None)
    if isinstance(data, (bytes, bytearray)):
        return bytes(data)
    if isinstance(data, str):
        return bytes.fromhex(data[2:] if data.startswith("0x") else data)
    # Older layout: exc.args[0] dict carrying 'data'.
    args = getattr(exc, "args", ())
    if args and isinstance(args[0], (bytes, bytearray)):
        return bytes(args[0])
    return None


def deploy(w3: Web3, private_key: str, bytecode: str) -> "CheckpointContract":
    """Deploy Checkpoints with an empty constructor and return the wrapper."""
    account = w3.eth.account.from_key(private_key)
    contract = w3.eth.contract(abi=load_abi(), bytecode=bytecode)
    tx = contract.constructor().build_transaction(
        {
            "from": account.address,
            "nonce": w3.eth.get_transaction_count(account.address),
            "gas": 3_000_000,
            "gasPrice": w3.eth.gas_price,
            "chainId": int(w3.eth.chain_id),
        }
    )
    signed = w3.eth.account.sign_transaction(tx, private_key)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash)
    if receipt["status"] != 1:
        raise RuntimeError("deployment transaction reverted")
    return CheckpointContract(w3, receipt["contractAddress"], private_key)
