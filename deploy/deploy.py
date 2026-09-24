"""部署 MerkleClaim 到本地 Anvil。

解决"叶子绑定合约地址"带来的循环依赖：
  CREATE 地址 = keccak256(rlp([deployer, nonce]))[12:]，可提前预测。
  因此：预测地址 → 用该地址+chainId 算 Merkle 根 → 发送部署交易，地址必然吻合。

用法：
  python -m deploy.deploy --allocations examples/allocations.json
  python -m deploy.deploy --allocations examples/allocations.json --fund-eth 10
  python -m deploy.deploy --allocations examples/allocations.json --token 0xERC20地址
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from eth_utils import keccak
from web3 import Web3
from web3.middleware import ExtraDataToPOAMiddleware

# 允许从仓库根目录直接运行
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend import config  # noqa: E402
from backend.chain import load_abi_and_bytecode  # noqa: E402
from backend.merkle import build_tree_from_allocations  # noqa: E402

ZERO = "0x0000000000000000000000000000000000000000"


def predict_create_address(deployer: str, nonce: int) -> str:
    """EIP-? 标准 CREATE 地址预测：RLP([sender, nonce]) 的 keccak 末 20 字节。"""
    import rlp  # 由 eth-account 间接依赖；若缺失则给明确提示

    raw = keccak(rlp.encode([Web3.to_bytes(hexstr=Web3.to_checksum_address(deployer)), nonce]))
    return Web3.to_checksum_address(raw[-20:])


def load_allocations(path: Path) -> list[dict]:
    data = json.loads(path.read_text())
    if isinstance(data, dict) and "allocations" in data:
        data = data["allocations"]
    out = []
    for a in data:
        out.append(
            {
                "index": int(a["index"]),
                "account": Web3.to_checksum_address(a["account"]),
                "amount": int(a["amount"]),
            }
        )
    return out


def deploy(rpc_url: str, private_key: str, allocations: list[dict],
           token: str = ZERO, fund_eth: float = 0.0,
           out_file: Path | None = None) -> dict:
    w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 60}))
    w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
    if not w3.is_connected():
        raise SystemExit(f"无法连接 {rpc_url}，请先启动 anvil")

    acct = w3.eth.account.from_key(private_key)
    chain_id = w3.eth.chain_id
    nonce = w3.eth.get_transaction_count(acct.address)

    predicted = predict_create_address(acct.address, nonce)
    tree, norm = build_tree_from_allocations(allocations, chain_id, predicted)
    root = tree.root

    abi, bytecode = load_abi_and_bytecode()
    Contract = w3.eth.contract(abi=abi, bytecode=bytecode)
    tx = Contract.constructor(root, Web3.to_checksum_address(token), acct.address).build_transaction(
        {
            "from": acct.address,
            "nonce": nonce,
            "chainId": chain_id,
            "gas": 3_000_000,
            "gasPrice": w3.eth.gas_price,
        }
    )
    signed = acct.sign_transaction(tx)
    h = w3.eth.send_raw_transaction(signed.raw_transaction)
    receipt = w3.eth.wait_for_transaction_receipt(h)
    addr = Web3.to_checksum_address(receipt["contractAddress"])
    assert addr == predicted, f"地址预测不一致: {addr} != {predicted}"

    # 链上校验根一致
    deployed = w3.eth.contract(address=addr, abi=abi)
    onchain_root = deployed.functions.merkleRoot().call()
    assert onchain_root == root, "链上根与本地根不一致"
    onchain_chain = deployed.functions.deployChainId().call()
    assert onchain_chain == chain_id

    info = {
        "address": addr,
        "root": "0x" + root.hex(),
        "chain_id": chain_id,
        "token": Web3.to_checksum_address(token),
        "owner": acct.address,
        "deployer": acct.address,
        "tx": h.hex(),
        "allocation_count": len(norm),
    }

    if fund_eth > 0:
        wei = w3.to_wei(fund_eth, "ether")
        fh = w3.eth.send_transaction({"from": acct.address, "to": addr, "value": wei})
        w3.eth.wait_for_transaction_receipt(fh)
        info["funded_eth"] = fund_eth

    if out_file:
        out_file.parent.mkdir(parents=True, exist_ok=True)
        out_file.write_text(json.dumps(info, indent=2))
        info["_saved_to"] = str(out_file)
    return info


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--rpc", default=config.RPC_URL)
    p.add_argument("--private-key", default=config.PRIVATE_KEY)
    p.add_argument("--allocations", required=True)
    p.add_argument("--token", default=ZERO)
    p.add_argument("--fund-eth", type=float, default=10.0, help="向合约转入的原生币（默认 10 ETH）")
    p.add_argument("--out", default=str(config.DEPLOY_INFO))
    args = p.parse_args()

    allocs = load_allocations(Path(args.allocations))
    info = deploy(args.rpc, args.private_key, allocs,
                  token=args.token, fund_eth=args.fund_eth, out_file=Path(args.out))
    print(json.dumps(info, indent=2))


if __name__ == "__main__":
    main()
