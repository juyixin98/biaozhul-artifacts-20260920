"""在本地 Anvil 上部署 BoundedSettlement + 两种模拟代币，并写 deployment.json。

前置：
  1. anvil 在 $BOUNDED_RPC_URL（默认 http://127.0.0.1:8545）上运行
  2. 已执行 `forge build`（out/ 目录存在）
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from web3 import Web3
from web3.contract import Contract

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_RPC_URL = "http://127.0.0.1:8545"

# Anvil 标准测试账户
DEPLOYER_PK = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
MAKER_PK = "0x59c6995e998f97a5a0044966f0945389dc9e86dae88c7a8412f4603b6b78690d"
TAKER_PK = "0x5de4111afa1a4b94908f83103eb1f1706367c2e68ca870fc3fb9a804cdab365a"
FEE_RECEIVER = "0x15d34AAf54267DB7D7c367839AAf71A00a2C6A65"  # anvil 账户 3

MINT = 1_000_000 * 10**18


def artifact(name: str) -> dict:
    path = ROOT / "out" / f"{name}.sol" / f"{name}.json"
    if not path.exists():
        sys.exit(f"找不到编译产物 {path}，请先运行 `forge build`")
    return json.loads(path.read_text())


def deploy(w3: Web3, deployer: str, name: str, *args) -> Contract:
    art = artifact(name)
    contract = w3.eth.contract(abi=art["abi"], bytecode=art["bytecode"]["object"])
    tx = contract.constructor(*args).build_transaction(
        {"from": deployer, "nonce": w3.eth.get_transaction_count(deployer), "chainId": w3.eth.chain_id}
    )
    return _send_deploy(w3, tx, DEPLOYER_PK, art["abi"])


def _send_deploy(w3: Web3, tx: dict, pk: str, abi: list) -> Contract:
    signed = w3.eth.account.sign_transaction(tx, pk)
    h = w3.eth.send_raw_transaction(signed.raw_transaction)
    rcpt = w3.eth.wait_for_transaction_receipt(h, timeout=30)
    if rcpt.status != 1:
        sys.exit(f"部署失败: {h.hex()}")
    return w3.eth.contract(address=rcpt.contractAddress, abi=abi)


def send(w3: Web3, contract: Contract, func, pk: str) -> None:
    sender = w3.eth.account.from_key(pk).address
    tx = func.build_transaction(
        {"from": sender, "nonce": w3.eth.get_transaction_count(sender), "chainId": w3.eth.chain_id}
    )
    signed = w3.eth.account.sign_transaction(tx, pk)
    h = w3.eth.send_raw_transaction(signed.raw_transaction)
    rcpt = w3.eth.wait_for_transaction_receipt(h, timeout=30)
    if rcpt.status != 1:
        sys.exit(f"交易失败: {h.hex()}")


def main() -> None:
    rpc_url = os.environ.get("BOUNDED_RPC_URL", DEFAULT_RPC_URL)
    w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 20}))
    if not w3.is_connected():
        sys.exit(f"无法连接 Anvil: {rpc_url}（请先启动 `anvil`）")
    chain_id = w3.eth.chain_id

    deployer = w3.eth.account.from_key(DEPLOYER_PK).address
    maker = w3.eth.account.from_key(MAKER_PK).address
    taker = w3.eth.account.from_key(TAKER_PK).address
    fee = Web3.to_checksum_address(FEE_RECEIVER)
    print(f"chainId={chain_id} deployer={deployer} maker={maker} taker={taker}")

    token_a = deploy(w3, deployer, "MockERC20", "Token A", "TKA")
    token_b = deploy(w3, deployer, "MockERC20", "Token B", "TKB")
    print(f"TokenA @ {token_a.address}")
    print(f"TokenB @ {token_b.address}")

    settlement = deploy(w3, deployer, "BoundedSettlement", fee)
    print(f"BoundedSettlement @ {settlement.address}")

    # maker 获得 100 万 TKA；taker 获得 100 万 TKB
    send(w3, token_a, token_a.functions.mint(maker, MINT), DEPLOYER_PK)
    send(w3, token_b, token_b.functions.mint(taker, MINT), DEPLOYER_PK)
    # maker/taker 对结算合约无限授权
    send(w3, token_a, token_a.functions.approve(settlement.address, 2**256 - 1), MAKER_PK)
    send(w3, token_b, token_b.functions.approve(settlement.address, 2**256 - 1), TAKER_PK)
    print("铸造与授权完成：maker 持有 TKA，taker 持有 TKB，均已 approve 结算合约")

    out = {
        "rpc_url": rpc_url,
        "chain_id": chain_id,
        "addresses": {
            "settlement": settlement.address,
            "tokenA": token_a.address,
            "tokenB": token_b.address,
        },
        "accounts": {
            "deployer": deployer,
            "maker": maker,
            "taker": taker,
            "feeReceiver": fee,
            "deployerPrivateKey": DEPLOYER_PK,
            "makerPrivateKey": MAKER_PK,
            "takerPrivateKey": TAKER_PK,
        },
    }
    (ROOT / "deployment.json").write_text(json.dumps(out, indent=2))
    print(f"已写入 {ROOT / 'deployment.json'}")


if __name__ == "__main__":
    main()
