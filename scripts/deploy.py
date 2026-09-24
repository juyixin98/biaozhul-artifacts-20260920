#!/usr/bin/env python3
"""部署 Checkpoints 合约到（本地）EVM 链并打印地址。

用法：
    python scripts/deploy.py
环境变量：RPC_URL、PRIVATE_KEY、ABI_PATH
"""
from __future__ import annotations

import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from web3 import Web3

from app.config import Settings


def main() -> None:
    s = Settings.from_env()
    w3 = Web3(Web3.HTTPProvider(s.rpc_url, request_kwargs={"timeout": 10}))
    if not w3.is_connected():
        raise SystemExit(f"无法连接节点 {s.rpc_url}，请先启动 Anvil")

    artifact_path = pathlib.Path(s.abi_path)
    artifact = json.loads(artifact_path.read_text())
    contract = w3.eth.contract(
        abi=artifact["abi"], bytecode=artifact["bytecode"]["object"]
    )
    acct = w3.eth.account.from_key(s.private_key)
    tx = contract.constructor().build_transaction(
        {
            "from": acct.address,
            "nonce": w3.eth.get_transaction_count(acct.address),
            "gas": 3_000_000,
            "gasPrice": w3.eth.gas_price,
            "chainId": w3.eth.chain_id,
        }
    )
    signed = acct.sign_transaction(tx)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=s.receipt_timeout)
    address = receipt["contractAddress"]
    print(f"chainId:    {w3.eth.chain_id}")
    print(f"deployer:   {acct.address}")
    print(f"contract:   {address}")
    print(f"tx:         {tx_hash.hex()}")
    print(f"gas used:   {receipt['gasUsed']}")
    print()
    print("启动 API：")
    print(f"  CONTRACT_ADDRESS={address} uvicorn app.main:app --reload")


if __name__ == "__main__":
    main()
