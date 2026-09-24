"""与本地 Anvil 链交互：连接、地址预测、部署、单笔/批量领取。"""

from __future__ import annotations

import json
from pathlib import Path

import rlp
from eth_account import Account
from eth_utils import keccak, to_checksum_address
from web3 import Web3
from web3.contract import Contract
from web3.types import TxParams, TxReceipt

from .allocations import Allocation
from .merkle import get_root, make_leaf


def connect(rpc_url: str) -> Web3:
    w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 30}))
    if not w3.is_connected():
        raise ConnectionError(f"cannot connect to Ethereum node at {rpc_url}")
    return w3


def load_contract(w3: Web3, address: str | None, artifact_path: str | Path) -> Contract | None:
    if not address:
        return None
    artifact = json.loads(Path(artifact_path).read_text(encoding="utf-8"))
    return w3.eth.contract(address=Web3.to_checksum_address(address), abi=artifact["abi"])


def predict_create_address(sender: str, nonce: int) -> str:
    """预测 EOA 用 CREATE 部署的合约地址（keccak256(rlp([sender, nonce]))[12:]）。"""
    sender_bytes = bytes.fromhex(sender[2:])
    raw = keccak(rlp.encode([sender_bytes, nonce]))
    return to_checksum_address(raw[12:])


def leaves_for(allocations: list[Allocation], chain_id: int, contract_address: str) -> list[bytes]:
    return [
        make_leaf(chain_id, contract_address, a.index, a.account, a.amount_wei)
        for a in allocations
    ]


def deploy_claim_contract(
    w3: Web3,
    deployer_private_key: str,
    allocations: list[Allocation],
    artifact_path: str | Path,
    fund_wei: int,
) -> tuple[Contract, str, bytes, int]:
    """部署 MerkleClaim。

    顺序：预测地址（当前 nonce）→ 以预测地址为域构造叶子和根 → 携带资金部署。
    返回 (合约对象, 地址, 根, 部署交易 nonce)。
    """
    artifact = json.loads(Path(artifact_path).read_text(encoding="utf-8"))
    abi = artifact["abi"]
    bytecode = artifact["bytecode"]["object"]

    deployer = Account.from_key(deployer_private_key).address
    nonce = w3.eth.get_transaction_count(deployer)
    chain_id = w3.eth.chain_id

    predicted = predict_create_address(deployer, nonce)
    root = get_root(leaves_for(allocations, chain_id, predicted))

    MerkleClaim = w3.eth.contract(abi=abi, bytecode=bytecode)
    tx = MerkleClaim.constructor(root).build_transaction(
        {
            "from": deployer,
            "nonce": nonce,
            "chainId": chain_id,
            "value": fund_wei,
            "gas": 3_000_000,
        }
    )
    receipt = _sign_and_send(w3, deployer_private_key, tx)
    address = receipt["contractAddress"]
    if to_checksum_address(address) != to_checksum_address(predicted):
        raise RuntimeError(
            f"deployment address mismatch: predicted {predicted}, got {address}"
        )
    return w3.eth.contract(address=address, abi=abi), address, root, nonce


def send_claim(
    w3: Web3,
    contract: Contract,
    private_key: str,
    index: int,
    account: str,
    amount: int,
    proof: list[bytes],
) -> TxReceipt:
    sender = Account.from_key(private_key).address
    tx = contract.functions.claim(
        index, Web3.to_checksum_address(account), amount, proof
    ).build_transaction(
        {
            "from": sender,
            "nonce": w3.eth.get_transaction_count(sender),
            "chainId": w3.eth.chain_id,
            "gas": 500_000,
        }
    )
    return _sign_and_send(w3, private_key, tx)


def send_claim_batch(
    w3: Web3,
    contract: Contract,
    private_key: str,
    indexes: list[int],
    accounts: list[str],
    amounts: list[int],
    proofs: list[list[bytes]],
) -> TxReceipt:
    """提交批量领取。

    发送前先 eth_estimateGas：当批次中任何一项证明无效 / 重复 / 转账失败时，
    节点对 revert 的估算会抛 ContractLogicError，我们转为 BatchRejected，
    绝不发出注定失败的交易。
    """
    sender = Account.from_key(private_key).address
    checksum_accounts = [Web3.to_checksum_address(a) for a in accounts]
    func = contract.functions.claimBatch(indexes, checksum_accounts, amounts, proofs)
    tx_params: TxParams = {
        "from": sender,
        "nonce": w3.eth.get_transaction_count(sender),
        "chainId": w3.eth.chain_id,
    }
    try:
        gas = func.estimate_gas({"from": sender, "value": 0})
    except Exception as exc:  # web3 抛 ContractLogicError（节点返回 execution revert）
        raise BatchRejected(str(exc)) from exc
    tx_params["gas"] = min(int(gas * 1.2), 15_000_000)
    tx = func.build_transaction(tx_params)
    return _sign_and_send(w3, private_key, tx)


class BatchRejected(Exception):
    """预估 gas 阶段节点就判定整批会 revert。"""


class TransactionReverted(RuntimeError):
    """交易已发出但链上执行 status=0（如重复领取、证明无效）。"""


def _sign_and_send(w3: Web3, private_key: str, tx: TxParams) -> TxReceipt:
    signed = w3.eth.account.sign_transaction(tx, private_key)
    raw = getattr(signed, "raw_transaction", None) or signed.rawTransaction
    tx_hash = w3.eth.send_raw_transaction(raw)  # 类型兼容老/新版本
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60, poll_latency=0.2)
    if receipt["status"] != 1:
        raise TransactionReverted(
            f"transaction reverted on-chain: {receipt['transactionHash'].hex()}"
        )
    return receipt


def get_balance(w3: Web3, address: str) -> int:
    return w3.eth.get_balance(Web3.to_checksum_address(address))
