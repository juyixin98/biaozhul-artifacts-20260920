"""web3.py client helpers: contract loading, deployment, transactions."""
import json
import os

from web3 import Web3

from app import config


def get_web3() -> Web3:
    w3 = Web3(Web3.HTTPProvider(config.RPC_URL, request_kwargs={"timeout": 30}))
    if not w3.is_connected():
        raise RuntimeError(f"cannot connect to {config.RPC_URL} (is anvil running?)")
    return w3


def load_artifact() -> dict:
    path = os.path.join(config.FORGE_OUT, "FeeSettlement.sol", "FeeSettlement.json")
    with open(path) as f:
        return json.load(f)


def load_abi():
    return load_artifact()["abi"]


def resolve_contract_address() -> str:
    if config.CONTRACT_ADDRESS:
        return Web3.to_checksum_address(config.CONTRACT_ADDRESS)
    if os.path.exists(config.DEPLOYMENT_FILE):
        with open(config.DEPLOYMENT_FILE) as f:
            return Web3.to_checksum_address(json.load(f)["address"])
    raise RuntimeError(
        "contract address unknown: set CONTRACT_ADDRESS or run scripts/deploy.py"
    )


def get_contract(w3: Web3):
    return w3.eth.contract(address=resolve_contract_address(), abi=load_abi())


def signer_address(w3: Web3) -> str:
    return w3.eth.account.from_key(config.PRIVATE_KEY).address


def send_tx(w3: Web3, fn, gas: int = 5_000_000):
    """Build, sign, send and wait for a contract transaction. Returns the receipt."""
    sender = signer_address(w3)
    tx = fn.build_transaction(
        {
            "from": sender,
            "nonce": w3.eth.get_transaction_count(sender),
            "gas": gas,
            "gasPrice": w3.eth.gas_price,
            "chainId": w3.eth.chain_id,
        }
    )
    signed = w3.eth.account.sign_transaction(tx, config.PRIVATE_KEY)
    raw = getattr(signed, "rawTransaction", None) or signed.raw_transaction
    tx_hash = w3.eth.send_raw_transaction(raw)
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash)
    if receipt.status != 1:
        raise RuntimeError(f"transaction reverted: {tx_hash.hex()}")
    return receipt


def deploy_contract(w3: Web3) -> str:
    """Deploy FeeSettlement from the forge artifact; returns the address."""
    artifact = load_artifact()
    factory = w3.eth.contract(abi=artifact["abi"], bytecode=artifact["bytecode"]["object"])
    receipt = send_tx(w3, factory.constructor(), gas=8_000_000)
    return receipt.contractAddress


def mine_blocks(w3: Web3, n: int):
    """Mine n empty blocks on the local anvil chain."""
    for _ in range(n):
        w3.provider.make_request("evm_mine", [])


def fees_limbs_of(account_tuple):
    """getAccount(...) -> (fees_limbs, fees_int)."""
    limbs = [int(account_tuple[i]) for i in range(5, 9)]
    value = sum(l << (128 * i) for i, l in enumerate(limbs))
    return limbs, value
