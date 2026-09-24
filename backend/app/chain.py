"""链交互层：读取 forge 产物、发送交易、整数算术计算份额/资产/滑点。

全程整数运算（Python int 为任意精度），不使用 float 表达金额或汇率。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from web3 import Web3
from web3.types import TxReceipt

from . import config


class ChainError(RuntimeError):
    """链上调用/交易失败。"""


@dataclass(frozen=True)
class ContractBundle:
    w3: Web3
    token: Any  # web3 contract
    vault: Any
    token_address: str
    vault_address: str
    chain_id: int


# ---------------------------------------------------------------------------
# ABI / 字节码
# ---------------------------------------------------------------------------

def _load_artifact(name: str) -> dict[str, Any]:
    path = config.FORGE_OUT / f"{name}.sol" / f"{name}.json"
    if not path.exists():
        raise ChainError(
            f"找不到合约产物 {path}。请先运行 `forge build`。"
        )
    with path.open() as fh:
        return json.load(fh)


def _load_deployment() -> dict[str, str]:
    if not config.DEPLOYMENT_FILE.exists():
        raise ChainError(
            f"找不到部署文件 {config.DEPLOYMENT_FILE}。请先运行 "
            "`python scripts/deploy.py`（或 `make deploy`）。"
        )
    with config.DEPLOYMENT_FILE.open() as fh:
        return json.load(fh)


# ---------------------------------------------------------------------------
# 连接 / 部署
# ---------------------------------------------------------------------------

def connect(rpc_url: str | None = None) -> Web3:
    w3 = Web3(Web3.HTTPProvider(rpc_url or config.RPC_URL, request_kwargs={"timeout": 20}))
    if not w3.is_connected():
        raise ChainError(f"无法连接 Anvil：{rpc_url or config.RPC_URL}")
    return w3


def deploy_contracts(rpc_url: str | None = None, private_key: str | None = None) -> dict[str, str]:
    """部署 MockERC20 + ShareVault 到本机 Anvil，并把地址写入 deployments/local.json。"""
    w3 = connect(rpc_url)
    pk = private_key or config.DEFAULT_PRIVATE_KEY
    acct = w3.eth.account.from_key(pk)

    token_art = _load_artifact("MockERC20")
    vault_art = _load_artifact("ShareVault")
    Token = w3.eth.contract(abi=token_art["abi"], bytecode=token_art["bytecode"]["object"])
    Vault = w3.eth.contract(abi=vault_art["abi"], bytecode=vault_art["bytecode"]["object"])

    token_addr = _send_deploy(w3, acct, Token.constructor("Mock Token", "MOCK", 18))
    token = w3.eth.contract(address=token_addr, abi=token_art["abi"])
    vault_addr = _send_deploy(
        w3, acct, Vault.constructor(token_addr, "Mock Share Vault", "sMOCK")
    )

    result = {
        "chain_id": w3.eth.chain_id,
        "rpc_url": rpc_url or config.RPC_URL,
        "token": w3.to_checksum_address(token_addr),
        "vault": w3.to_checksum_address(vault_addr),
        "deployer": acct.address,
    }
    config.DEPLOYMENT_FILE.parent.mkdir(parents=True, exist_ok=True)
    with config.DEPLOYMENT_FILE.open("w") as fh:
        json.dump(result, fh, indent=2)
    return result


def _send_deploy(w3: Web3, acct, constructor) -> str:
    tx = constructor.build_transaction(
        {
            "from": acct.address,
            "nonce": w3.eth.get_transaction_count(acct.address),
            "gas": 4_000_000,
            "maxFeePerGas": w3.to_wei(20, "gwei"),
            "maxPriorityFeePerGas": w3.to_wei(1, "gwei"),
            "chainId": w3.eth.chain_id,
        }
    )
    signed = acct.sign_transaction(tx)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    rcpt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=30)
    if rcpt.status != 1:
        raise ChainError("合约部署交易失败")
    if not rcpt.contractAddress:
        raise ChainError("部署交易未产生合约地址")
    return rcpt.contractAddress


def load_bundle(rpc_url: str | None = None) -> ContractBundle:
    w3 = connect(rpc_url)
    dep = _load_deployment()
    token_abi = _load_artifact("MockERC20")["abi"]
    vault_abi = _load_artifact("ShareVault")["abi"]
    token = w3.eth.contract(address=Web3.to_checksum_address(dep["token"]), abi=token_abi)
    vault = w3.eth.contract(address=Web3.to_checksum_address(dep["vault"]), abi=vault_abi)
    return ContractBundle(
        w3=w3,
        token=token,
        vault=vault,
        token_address=dep["token"],
        vault_address=dep["vault"],
        chain_id=w3.eth.chain_id,
    )


# ---------------------------------------------------------------------------
# 整数算术（与合约公式严格一致；Python // 为向下取整，对非负数与 EVM 相同）
# ---------------------------------------------------------------------------

def apply_slippage_floor(exact: int, slippage_bps: int) -> int:
    """用户容忍 slippage_bps 基点的不利偏差，返回可接受的最小结果。"""
    if not 0 <= slippage_bps < 10_000:
        raise ValueError("slippage_bps 必须在 [0, 10000) 区间")
    return (exact * (10_000 - slippage_bps)) // 10_000


# ---------------------------------------------------------------------------
# 交易发送（统一 nonce/gas 管理与错误解码）
# ---------------------------------------------------------------------------

def _send(w3: Web3, acct, func, value: int = 0, gas: int = 1_000_000) -> TxReceipt:
    tx = func.build_transaction(
        {
            "from": acct.address,
            "nonce": w3.eth.get_transaction_count(acct.address),
            "gas": gas,
            "maxFeePerGas": w3.to_wei(20, "gwei"),
            "maxPriorityFeePerGas": w3.to_wei(1, "gwei"),
            "chainId": w3.eth.chain_id,
            "value": value,
        }
    )
    signed = acct.sign_transaction(tx)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    rcpt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=30)
    if rcpt.status != 1:
        # 用 call 重放以拿到合约 revert 原因。
        try:
            func.call({"from": acct.address})
        except Exception as exc:  # noqa: BLE001 - 仅用于错误信息
            raise ChainError(f"交易回滚：{exc}") from exc
        raise ChainError("交易回滚（原因未知）")
    return rcpt
