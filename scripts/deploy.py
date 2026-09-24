#!/usr/bin/env python3
"""部署 MultiSigTimelock + Counter 到本地 Anvil。

用法:
    python scripts/deploy.py [--delay 3600] [--cooldown 600] \
        [--threshold 2] [--out deployments.json]

默认使用 Anvil 内置测试账户（仅用于本地测试，切勿用于真实网络）。
输出 JSON：wallet / counter 地址、链 ID、签名人、阈值、延时配置。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from web3 import Web3

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "contracts" / "out"

# Anvil 官方测试账户（公开、无资金安全假设）
DEFAULT_ANVIL_KEYS = [
    "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80",
    "0x59c6995e998f97a5a0044966f0945389dc9e86dae88c7a8412f4603b6b78690d",
    "0x5de4111afa1a4b94908f83103eb1f1706367c2e68ca870fc3fb9a804cdab365a",
    "0x7c852118294e51e653712a81e05800f419141751be58f605c371e15141b007a6",
    "0x47e179ec197488593b187f80a00eb0da91f1b9d0b13f8733639f19c30a34926a",
]


def load_abi(name: str) -> list:
    p = OUT_DIR / f"{name}.sol" / f"{name}.json"
    if not p.exists():
        sys.exit(f"找不到 {p}，请先在 contracts/ 下执行 forge build")
    return json.loads(p.read_text())["abi"]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rpc", default=os.environ.get("RPC_URL", "http://127.0.0.1:8545"))
    ap.add_argument("--delay", type=int, default=int(os.environ.get("TIMELOCK_DELAY", "3600")))
    ap.add_argument("--cooldown", type=int, default=int(os.environ.get("RETRY_COOLDOWN", "600")))
    ap.add_argument("--threshold", type=int, default=int(os.environ.get("THRESHOLD", "2")))
    ap.add_argument("--signer-count", type=int, default=3)
    ap.add_argument("--counter-fail-until", type=int, default=0,
                    help="Counter 在 (部署时间+该秒数) 前调用失败，用于重试演示；0=从不失败")
    ap.add_argument("--out", default=str(ROOT / "deployments.json"))
    args = ap.parse_args()

    w3 = Web3(Web3.HTTPProvider(args.rpc, request_kwargs={"timeout": 10}))
    if not w3.is_connected():
        sys.exit(f"无法连接本地链 {args.rpc}，请先启动 anvil")

    deployer = w3.eth.account.from_key(DEFAULT_ANVIL_KEYS[0])
    chain_id = w3.eth.chain_id

    if args.signer_count > len(DEFAULT_ANVIL_KEYS):
        sys.exit("测试密钥不足")
    signer_addrs = [
        w3.eth.account.from_key(k).address for k in DEFAULT_ANVIL_KEYS[: args.signer_count]
    ]
    if not (1 <= args.threshold <= len(signer_addrs)):
        sys.exit("阈值必须在 1..签名人数 之间")

    def deploy(contract):
        tx = contract.constructor().build_transaction({
            "from": deployer.address,
            "nonce": w3.eth.get_transaction_count(deployer.address),
            "gas": 3_000_000,
            "gasPrice": w3.eth.gas_price,
            "chainId": chain_id,
        })
        signed = deployer.sign_transaction(tx)
        h = w3.eth.send_raw_transaction(signed.raw_transaction)
        rc = w3.eth.wait_for_transaction_receipt(h)
        if rc["status"] != 1:
            sys.exit(f"部署失败: {h.hex()}")
        return rc["contractAddress"], h.hex()

    # 先部署 wallet，再把 wallet 地址传给 Counter
    wallet_abi = load_abi("MultiSigTimelock")
    counter_abi = load_abi("Counter")

    Wallet = w3.eth.contract(abi=wallet_abi, bytecode=json.loads(
        (OUT_DIR / "MultiSigTimelock.sol" / "MultiSigTimelock.json").read_text())["bytecode"]["object"])
    tx = Wallet.constructor(signer_addrs, args.threshold, args.delay, args.cooldown).build_transaction({
        "from": deployer.address,
        "nonce": w3.eth.get_transaction_count(deployer.address),
        "gas": 3_000_000,
        "gasPrice": w3.eth.gas_price,
        "chainId": chain_id,
    })
    signed = deployer.sign_transaction(tx)
    h = w3.eth.send_raw_transaction(signed.raw_transaction)
    rc = w3.eth.wait_for_transaction_receipt(h)
    if rc["status"] != 1:
        sys.exit(f"wallet 部署失败: {h.hex()}")
    wallet_addr = rc["contractAddress"]

    Counter = w3.eth.contract(abi=counter_abi, bytecode=json.loads(
        (OUT_DIR / "Counter.sol" / "Counter.json").read_text())["bytecode"]["object"])
    fail_until = args.counter_fail_until
    succeed_after = (w3.eth.get_block("latest")["timestamp"] + fail_until) if fail_until else 0
    tx = Counter.constructor(wallet_addr, succeed_after).build_transaction({
        "from": deployer.address,
        "nonce": w3.eth.get_transaction_count(deployer.address),
        "gas": 3_000_000,
        "gasPrice": w3.eth.gas_price,
        "chainId": chain_id,
    })
    signed = deployer.sign_transaction(tx)
    h = w3.eth.send_raw_transaction(signed.raw_transaction)
    rc = w3.eth.wait_for_transaction_receipt(h)
    counter_addr = rc["contractAddress"]

    result = {
        "chain_id": chain_id,
        "rpc_url": args.rpc,
        "wallet": Web3.to_checksum_address(wallet_addr),
        "counter": Web3.to_checksum_address(counter_addr),
        "threshold": args.threshold,
        "delay": args.delay,
        "retry_cooldown": args.cooldown,
        "signers": [Web3.to_checksum_address(a) for a in signer_addrs],
        "signer_keys": DEFAULT_ANVIL_KEYS[: args.signer_count],
        "deployer": deployer.address,
    }
    Path(args.out).write_text(json.dumps(result, indent=2))
    print(json.dumps({k: v for k, v in result.items() if k != "signer_keys"}, indent=2))
    print(f"\n部署信息已写入 {args.out}")


if __name__ == "__main__":
    main()
