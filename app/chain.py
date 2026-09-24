"""web3.py chain client and Ledger log decoding.

Only the handful of JSON-RPC calls the indexer needs is exposed. Logs are
decoded manually (topic0 signature + fixed-width data words) so behavior does
not depend on the exact web3.py contract-event API surface (which differs
between web3 v5/v6/v7).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from eth_abi import decode as abi_decode
from eth_utils import keccak, to_checksum_address
from web3 import Web3

# 32-byte topic0 -> event name.
_EVENT_SIGNATURES = {
    "Deposited(address,uint256,uint256)": "Deposited",
    "Withdrawn(address,uint256,uint256)": "Withdrawn",
    "Transferred(address,address,uint256,uint256)": "Transferred",
}
TOPIC_TO_EVENT: dict[str, str] = {
    "0x" + keccak(text=sig).hex(): name for sig, name in _EVENT_SIGNATURES.items()
}


@dataclass(frozen=True)
class BlockHeader:
    number: int
    hash: str
    parent_hash: str


def to_hex(value: bytes | str) -> str:
    if isinstance(value, str):
        return value if value.startswith("0x") else "0x" + value
    return "0x" + bytes(value).hex()


class ChainClient:
    def __init__(self, rpc_url: str, contract_address: str | None = None):
        self.rpc_url = rpc_url
        self.w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 10}))
        self.contract_address = (
            to_checksum_address(contract_address) if contract_address else None
        )

    # ------------------------------------------------------------- basic

    def is_connected(self) -> bool:
        return self.w3.is_connected()

    def chain_id(self) -> int:
        return self.w3.eth.chain_id

    def head(self) -> BlockHeader:
        b = self.w3.eth.get_block("latest")
        return BlockHeader(
            number=b["number"],
            hash=to_hex(b["hash"]),
            parent_hash=to_hex(b["parentHash"]),
        )

    def get_header(self, block_number: int) -> BlockHeader:
        b = self.w3.eth.get_block(block_number)
        return BlockHeader(
            number=b["number"],
            hash=to_hex(b["hash"]),
            parent_hash=to_hex(b["parentHash"]),
        )

    def get_header_by_hash(self, block_hash: str) -> BlockHeader:
        b = self.w3.eth.get_block(block_hash)
        return BlockHeader(
            number=b["number"],
            hash=to_hex(b["hash"]),
            parent_hash=to_hex(b["parentHash"]),
        )

    # ------------------------------------------------------------- logs

    def fetch_logs(self, from_block: int, to_block: int) -> list[dict]:
        """Fetch decoded Ledger logs for [from_block, to_block] (inclusive)."""
        if self.contract_address is None:
            return []
        filter_params = {
            "fromBlock": from_block,
            "toBlock": to_block,
            "address": self.contract_address,
            "topics": [list(TOPIC_TO_EVENT)],
        }
        raw_logs = self.w3.eth.get_logs(filter_params)
        decoded: list[dict] = []
        for log in raw_logs:
            event = self._decode_log(log)
            if event is not None:
                decoded.append(event)
        return decoded

    def fetch_logs_chunked(
        self, from_block: int, to_block: int, chunk_size: int = 100
    ) -> list[dict]:
        out: list[dict] = []
        start = from_block
        while start <= to_block:
            end = min(start + chunk_size - 1, to_block)
            out.extend(self.fetch_logs(start, end))
            start = end + 1
        out.sort(key=lambda e: (e["block_number"], e["log_index"]))
        return out

    def _decode_log(self, log) -> dict | None:
        topics = [to_hex(t) for t in log["topics"]]
        name = TOPIC_TO_EVENT.get(topics[0])
        if name is None:
            return None
        amount, tag = abi_decode(["uint256", "uint256"], bytes(log["data"]))
        account = to_checksum_address("0x" + topics[1][-40:])
        to_account = None
        if name == "Transferred":
            to_account = to_checksum_address("0x" + topics[2][-40:])
        return {
            "name": name,
            "account": account,
            "to_account": to_account,
            "amount": int(amount),
            "tag": int(tag),
            "tx_hash": to_hex(log["transactionHash"]),
            "log_index": int(log["logIndex"]),
            "block_hash": to_hex(log["blockHash"]),
            "block_number": int(log["blockNumber"]),
        }

    # ------------------------------------------------------------ helpers

    @staticmethod
    def load_bytecode(build_path: str | Path | None = None) -> bytes:
        """Read deployed bytecode from the forge artifact."""
        if build_path is None:
            repo_root = Path(__file__).resolve().parents[1]
            build_path = repo_root / "contracts" / "out" / "Ledger.sol" / "Ledger.json"
        artifact = json.loads(Path(build_path).read_text())
        return bytes.fromhex(artifact["deployedBytecode"]["object"][2:])
