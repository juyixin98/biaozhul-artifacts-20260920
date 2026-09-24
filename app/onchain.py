"""Local-chain (Anvil) helpers built on web3.py.

Everything here talks HTTP JSON-RPC to a local Anvil started with
``--no-mining`` (manual block production) so tests can grow competing chains
for reorg scenarios. Only well-known Anvil test keys are used.
"""
from __future__ import annotations

import threading
from collections import defaultdict
from typing import Any

from web3 import Web3
from web3.providers.rpc import HTTPProvider

from .abi import VAULT_ABI, load_artifact

# First ten standard Anvil/Hardhat test accounts, key -> address derived below.
ANVIL_TEST_KEYS = [
    "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80",
    "0x59c6995e998f97a5a0044966f0945389dc9e86dae88c7a8412f4603b6b78690d",
    "0x5de4111afa1a4b94908f83103eb1f1706367c2e68ca870fc3fb9a804cdab365a",
    "0x7c852118294e51e653712a81e05800f419141751be58f605c371e15141b007a6",
    "0x47e179ec197488593b187f80a00eb0da91f1b9d0b13f8733639f19c30a34926a",
    "0x8b3a350cf5c34c9194ca85829a2df0ec3153be0318b5e2d3348e872092edffba",
    "0x92db14e403b83dfe3df233f83dfa3a0d7096f21ca9b0d6d6b8d88b2b4ec1564e",
    "0x4bbbf85ce3377467afe5d46f804f221813b2bb87f24d81f60f1fcdbf7cbf4356",
    "0xdbda1821b80551c9d65939329250298aa3472ba22feea921c0cf5d620ea67b97",
    "0x2a871d0798f97d79848a013d4936a73bf4cc922c825d33c1cf7073dff6d409c6",
]


def make_w3(rpc_url: str = "http://127.0.0.1:8545", timeout: int = 30) -> Web3:
    provider = HTTPProvider(rpc_url, request_kwargs={"timeout": timeout})
    w3 = Web3(provider)
    if not w3.is_connected():
        raise ConnectionError(f"cannot connect to Anvil at {rpc_url}")
    return w3


def address_for(key: str) -> str:
    acct = Web3().eth.account.from_key(key)
    return acct.address


# ---- mining / snapshots ---------------------------------------------------

def mine_blocks(w3: Web3, count: int = 1, timestamps: list[int] | None = None) -> list[str]:
    """Mine *count* blocks; return their hashes.

    If *timestamps* is given, set each next block's timestamp explicitly
    (values must be strictly increasing, used to force distinct block hashes
    for identical transaction content across a reorg)."""
    for i in range(count):
        if timestamps is not None and i < len(timestamps):
            w3.provider.make_request("evm_setNextBlockTimestamp", [timestamps[i]])
        w3.provider.make_request("evm_mine", [])
    head = w3.eth.block_number
    hashes = []
    for n in range(head - count + 1, head + 1):
        hashes.append(w3.eth.get_block(n)["hash"].to_0x_hex())
    return hashes


def snapshot(w3: Web3):
    resp = w3.provider.make_request("evm_snapshot", [])
    return resp["result"]


def revert(w3: Web3, snapshot_id) -> bool:
    resp = w3.provider.make_request("evm_revert", [snapshot_id])
    return bool(resp.get("result"))


def set_balance(w3: Web3, addr: str, wei: int) -> None:
    w3.provider.make_request("anvil_setBalance", [addr, hex(wei)])


# ---- transactions ---------------------------------------------------------

class TxSender:
    """Sends legacy signed txs under manual mining, tracking nonces locally.

    One sender per Web3 instance; nonces advance synchronously so several
    transactions can be queued and then included in a single ``evm_mine``.
    """

    def __init__(self, w3: Web3, gas_price: int | None = None):
        self.w3 = w3
        self.chain_id = w3.eth.chain_id
        # Fixed gas price makes identical (nonce,to,value,data) transactions
        # hash identically even when they are sent on different forks.
        self.gas_price = gas_price if gas_price is not None else w3.eth.gas_price
        self._nonce: dict[str, int] = defaultdict(lambda: -1)
        self._lock = threading.Lock()

    def _next_nonce(self, addr: str) -> int:
        on_chain = self.w3.eth.get_transaction_count(addr, "pending")
        if self._nonce[addr] < on_chain:
            self._nonce[addr] = on_chain
        else:
            self._nonce[addr] += 1
        return self._nonce[addr]

    def reset_nonce_tracking(self, addr: str | None = None) -> None:
        """Forget cached nonces (e.g. after ``evm_revert``); re-read from chain."""
        with self._lock:
            if addr is None:
                self._nonce.clear()
            else:
                self._nonce[Web3.to_checksum_address(addr)] = -1

    def send(
        self,
        from_key: str,
        to: str | None = None,
        value: int = 0,
        data: bytes = b"",
        gas: int = 400_000,
    ) -> str:
        with self._lock:
            acct = self.w3.eth.account.from_key(from_key)
            tx = {
                "nonce": self._next_nonce(acct.address),
                "gasPrice": self.gas_price,
                "gas": gas,
                "chainId": self.chain_id,
                "value": value,
                "data": data,
            }
            if to is not None:
                tx["to"] = Web3.to_checksum_address(to)
            signed = self.w3.eth.account.sign_transaction(tx, from_key)
            raw = signed.raw_transaction.hex()
            if not raw.startswith("0x"):
                raw = "0x" + raw
            resp = self.w3.provider.make_request("eth_sendRawTransaction", [raw])
            if "error" in resp:
                raise RuntimeError(f"sendRawTransaction failed: {resp['error']}")
            return resp["result"]

    def deploy(self, from_key: str, bytecode: str) -> str:
        return self.send(from_key, to=None, data=bytes.fromhex(bytecode.removeprefix("0x")))

    def vault_call(self, from_key: str, vault, fn_name: str, *args, value: int = 0) -> str:
        data = vault.encode_abi(fn_name, args=list(args))
        return self.send(from_key, to=vault.address, value=value, data=bytes.fromhex(data[2:]))


def deploy_vault(w3: Web3, sender: TxSender, deployer_key: str) -> str:
    artifact = load_artifact()
    tx_hash = sender.deploy(deployer_key, artifact["bytecode"]["object"])
    mine_blocks(w3, 1)
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash)
    return receipt["contractAddress"]


def vault_at(w3: Web3, address: str):
    return w3.eth.contract(address=Web3.to_checksum_address(address), abi=VAULT_ABI)


# ---- read helpers ---------------------------------------------------------

def block_info(w3: Web3, number: int) -> dict[str, Any]:
    b = w3.eth.get_block(number)
    return {
        "number": int(b["number"]),
        "hash": b["hash"].to_0x_hex(),
        "parent_hash": b["parentHash"].to_0x_hex(),
        "timestamp": int(b["timestamp"]),
        "transactions": [t.to_0x_hex() for t in b["transactions"]],
    }


def vault_logs(w3: Web3, address: str, from_block: int, to_block: int) -> list[dict[str, Any]]:
    """Raw Vault logs for a block range, in chain order."""
    result = w3.eth.get_logs(
        {
            "address": Web3.to_checksum_address(address),
            "fromBlock": hex(from_block),
            "toBlock": hex(to_block),
        }
    )
    out = []
    for log in result:
        out.append(
            {
                "address": log["address"],
                "topics": [
                    t.to_0x_hex() if hasattr(t, "to_0x_hex") else str(t)
                    for t in log["topics"]
                ],
                "data": log["data"].to_0x_hex() if hasattr(log["data"], "to_0x_hex") else log["data"],
                "blockNumber": int(log["blockNumber"]),
                "blockHash": log["blockHash"].to_0x_hex(),
                "transactionHash": log["transactionHash"].to_0x_hex(),
                "transactionIndex": int(log["transactionIndex"]),
                "logIndex": int(log["logIndex"]),
            }
        )
    out.sort(key=lambda l: (l["blockNumber"], l["transactionIndex"], l["logIndex"]))
    return out
