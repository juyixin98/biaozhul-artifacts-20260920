#!/usr/bin/env python3
"""同块更新合并的端到端演示：

关闭 automine → 在同一区块连续提交 N 笔 setValue → 挖一个块统一打包，
然后通过 HTTP 接口验证只产生一个检查点、值为最后一次写入。

用法：
    RPC_URL=http://127.0.0.1:8547 \
    CONTRACT_ADDRESS=0x... python scripts/demo_same_block.py
"""
from __future__ import annotations

import os
import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import httpx
from web3 import Web3

from app.config import DEFAULT_ANVIL_PRIVATE_KEY, Settings


def main() -> None:
    s = Settings.from_env()
    w3 = Web3(Web3.HTTPProvider(s.rpc_url))
    api_url = os.getenv("API_URL", "http://127.0.0.1:8001")

    import json

    artifact = json.loads(pathlib.Path(s.abi_path).read_text())
    contract = w3.eth.contract(
        address=Web3.to_checksum_address(s.contract_address), abi=artifact["abi"]
    )
    acct = w3.eth.account.from_key(s.private_key)

    def fire(value: int) -> None:
        nonce = w3.eth.get_transaction_count(acct.address, "pending")
        tx = contract.functions.setValue(value).build_transaction(
            {
                "from": acct.address,
                "nonce": nonce,
                "gas": 300_000,
                # gasPrice 随 nonce 微增，避免 Anvil 判为 underpriced 替代交易
                "gasPrice": w3.eth.gas_price + nonce,
                "chainId": w3.eth.chain_id,
            }
        )
        signed = acct.sign_transaction(tx)
        w3.eth.send_raw_transaction(signed.raw_transaction)

    target_block = w3.eth.block_number + 1
    n = 50
    print(f"关闭 automine，连续提交 {n} 笔 setValue(1..{n})，目标块 {target_block}")
    w3.provider.make_request("evm_setAutomine", [False])
    try:
        for v in range(1, n + 1):
            fire(v)
        w3.provider.make_request("evm_mine", [])  # 统一打包进一个新块
    finally:
        w3.provider.make_request("evm_setAutomine", [True])

    history = httpx.get(f"{api_url}/history", timeout=10).json()
    print("HTTP GET /history 检查点数量：", len(history))
    print("最后一个检查点：", history[-1])

    r = httpx.get(f"{api_url}/checkpoints/{target_block}", timeout=10)
    print(f"HTTP GET /checkpoints/{target_block} ->", r.json())

    assert w3.eth.block_number == target_block
    assert history[-1]["blockNumber"] == target_block
    assert history[-1]["value"] == n, "同块合并后值应为最后一次写入"
    print("✓ 同块更新合并验证通过")


if __name__ == "__main__":
    main()
