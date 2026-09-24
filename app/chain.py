"""链上 Checkpoints 合约的 web3.py 客户端封装。"""
from __future__ import annotations

import json
import pathlib
from typing import Any

from web3 import Web3
from web3.types import TxReceipt


class FutureBlockError(Exception):
    """查询目标区块晚于链上当前区块。"""

    def __init__(self, requested: int, current: int) -> None:
        self.requested = requested
        self.current = current
        super().__init__(
            f"target block {requested} is in the future (current block: {current})"
        )


class RpcUnavailableError(Exception):
    """无法连接到本地区块链节点。"""


def load_contract(
    w3: Web3, address: str | None, abi_path: str
) -> Any:
    """按地址与 forge 产物中的 ABI 加载合约句柄。"""
    if not address:
        raise ValueError(
            "CONTRACT_ADDRESS is not set; run scripts/deploy.py first"
        )
    artifact = json.loads(pathlib.Path(abi_path).read_text())
    abi = artifact["abi"]
    return w3.eth.contract(address=Web3.to_checksum_address(address), abi=abi)


class CheckpointClient:
    """对 Checkpoints 合约的读/写封装。"""

    def __init__(self, w3: Web3, contract: Any, private_key: str,
                 receipt_timeout: float = 30.0) -> None:
        self.w3 = w3
        self.contract = contract
        self.acct = w3.eth.account.from_key(private_key)
        self.receipt_timeout = receipt_timeout

    # ------------------------------------------------------------------
    # 写入
    # ------------------------------------------------------------------

    def set_value(self, value: int) -> TxReceipt:
        """发送 setValue 交易并等待回执（同区块合并由合约处理）。"""
        tx = self.contract.functions.setValue(value).build_transaction(
            {
                "from": self.acct.address,
                "nonce": self.w3.eth.get_transaction_count(self.acct.address),
                "gas": 300_000,
                "gasPrice": self.w3.eth.gas_price,
                "chainId": self.w3.eth.chain_id,
            }
        )
        signed = self.acct.sign_transaction(tx)
        tx_hash = self.w3.eth.send_raw_transaction(signed.raw_transaction)
        return self.w3.eth.wait_for_transaction_receipt(
            tx_hash, timeout=self.receipt_timeout
        )

    # ------------------------------------------------------------------
    # 读取
    # ------------------------------------------------------------------

    def value_at(self, target_block: int) -> int:
        """查询不晚于 target_block 的最后值；目标块在未来则抛出 FutureBlockError。"""
        current = self.w3.eth.block_number
        if target_block > current:
            raise FutureBlockError(target_block, current)
        try:
            return self.contract.functions.valueAt(target_block).call()
        except Exception as exc:  # 合约也会独立拒绝（防御性双重检查）
            if self._is_future_block_revert(exc):
                raise FutureBlockError(target_block, current) from exc
            raise

    def latest(self) -> dict[str, int]:
        block_number, value = self.contract.functions.latest().call()
        return {"blockNumber": block_number, "value": value}

    def history(self) -> list[dict[str, int]]:
        """链下遍历全部检查点（用于与参考实现核对）。"""
        n = self.contract.functions.length().call()
        result: [] = []
        for i in range(n):
            b, v = self.contract.functions.checkpointAt(i).call()
            result.append({"blockNumber": b, "value": v})
        return result

    @staticmethod
    def _is_future_block_revert(exc: Exception) -> bool:
        # 合约自定义错误 FutureBlock(uint256,uint224/uint256) 的选择器前 4 字节
        # keccak("FutureBlock(uint256,uint256)")[:4]
        msg = str(exc)
        return "FutureBlock" in msg or "0x59f13e21" in msg
