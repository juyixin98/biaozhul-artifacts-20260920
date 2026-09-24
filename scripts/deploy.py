"""Deploy BoundedOrderSettlement + two mock tokens to a local Anvil node.

Usage:
    python -m scripts.deploy                 # uses RPC_URL / defaults
    RPC_URL=http://127.0.0.1:8545 python -m scripts.deploy

The script is idempotent in the sense that it always deploys FRESH contracts
and overwrites deployment.json. Test keys only.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from web3 import Web3  # noqa: E402

from app.abi import erc20_abi, settlement_abi  # noqa: E402
from app.chain import LocalChain  # noqa: E402
from app.config import Settings  # noqa: E402

SETTLEMENT_BYTECODE_PATH = Path("out/BoundedOrderSettlement.sol/BoundedOrderSettlement.json")
ERC20_BYTECODE_PATH = Path("out/MockERC20.sol/MockERC20.json")
MAX_UINT256 = 2**256 - 1


def _bytecode(rel: Path) -> str:
    with (Path(__file__).resolve().parent.parent / rel).open() as fh:
        return json.load(fh)["bytecode"]["object"]


def deploy(chain: LocalChain, deployer_key: str, contract_name: str,
           abi: list, bytecode: str, *constructor_args) -> str:
    acct = chain.w3.eth.account.from_key(deployer_key)
    contract = chain.w3.eth.contract(abi=abi, bytecode=bytecode)
    data = contract.constructor(*constructor_args).build_transaction({
        "from": acct.address,
        "nonce": chain.w3.eth.get_transaction_count(acct.address, "pending"),
        "chainId": chain.chain_id,
        "gas": 3_000_000,
        "maxFeePerGas": chain.w3.to_wei(20, "gwei"),
        "maxPriorityFeePerGas": chain.w3.to_wei(1, "gwei"),
    })["data"]
    # Use the shared sender to keep nonce handling in one place.
    tx = chain.send_tx(deployer_key, to=None, data=bytes.fromhex(data.removeprefix("0x")))
    receipt = tx.receipt
    addr = receipt["contractAddress"]
    if not addr:
        raise RuntimeError(f"{contract_name} deployment produced no address")
    print(f"  deployed {contract_name:28s} -> {addr} (gas {tx.gas_used})")
    return addr


def main() -> int:
    settings = Settings.from_env()
    print(f"Connecting to {settings.rpc_url} (expected chain id {settings.chain_id}) ...")
    chain = LocalChain(settings.rpc_url)
    if chain.chain_id != settings.chain_id:
        print(f"WARNING: node chain id {chain.chain_id} != configured {settings.chain_id}")

    deployer_key = settings.role_keys["deployer"]
    deployer = chain.address_of(deployer_key)
    print(f"Deployer: {deployer}  ETH: {Web3.from_wei(chain.balance_eth(deployer), 'ether')}")

    settlement_addr = deploy(
        chain, deployer_key, "BoundedOrderSettlement",
        settlement_abi(), _bytecode(SETTLEMENT_BYTECODE_PATH),
    )
    token_a = deploy(
        chain, deployer_key, "MockERC20 TokenA (MKA)",
        erc20_abi(), _bytecode(ERC20_BYTECODE_PATH),
        "Mock Token A", "MKA", 18,
    )
    token_b = deploy(
        chain, deployer_key, "MockERC20 TokenB (MKB)",
        erc20_abi(), _bytecode(ERC20_BYTECODE_PATH),
        "Mock Token B", "MKB", 18,
    )

    maker = chain.address_of(settings.role_keys["maker"])
    taker = chain.address_of(settings.role_keys["taker"])

    def mint(token: str, to: str, amount: int) -> None:
        c = chain.erc20(token)
        tx = chain.send_tx(
            deployer_key, to=token,
            data=c.encode_abi("mint", args=[Web3.to_checksum_address(to), amount]),
        )
        print(f"  mint {amount} -> {to} on {token} (gas {tx.gas_used})")

    initial = 1_000_000 * 10**18
    print("Minting test balances ...")
    mint(token_a, maker, initial)
    mint(token_b, taker, initial)

    print("Approving settlement to pull tokens ...")
    for token, key, who in (
        (token_a, settings.role_keys["maker"], maker),
        (token_b, settings.role_keys["taker"], taker),
    ):
        tx = chain.token_approve(token, key, settlement_addr, MAX_UINT256)
        print(f"  approve {token} for {who} (gas {tx.gas_used})")

    record = {
        "deployed_at": int(time.time()),
        "chain_id": chain.chain_id,
        "rpc_url": settings.rpc_url,
        "settlement": settlement_addr,
        "tokenA": token_a,
        "tokenB": token_b,
        "accounts": {
            "deployer": deployer,
            "maker": maker,
            "taker": taker,
            "feeRecipient": chain.address_of(settings.role_keys["feeRecipient"]),
        },
    }
    settings.deployment_file.write_text(json.dumps(record, indent=2) + "\n")
    print(f"\nWrote {settings.deployment_file}")
    print(json.dumps(record, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
