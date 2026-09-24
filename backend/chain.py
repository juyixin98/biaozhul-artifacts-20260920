"""web3.py 链上客户端：连接本地 Anvil、加载 ABI、发送批量领取交易。"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Tuple

from web3 import Web3
from web3.middleware import ExtraDataToPOAMiddleware

from . import config

ABI: List[Dict[str, Any]] | None = None
_artifact_mtime: float = -1.0


def get_web3() -> Web3:
    w3 = Web3(Web3.HTTPProvider(config.RPC_URL, request_kwargs={"timeout": config.TX_TIMEOUT}))
    # Anvil/Geth 的 extraData 兼容
    w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
    if not w3.is_connected():
        raise ConnectionError(f"无法连接本地链 {config.RPC_URL}（请先启动 anvil）")
    return w3


def load_abi() -> List[Dict[str, Any]]:
    """读取 forge build 产物中的 ABI（带 mtime 缓存）。"""
    global ABI, _artifact_mtime
    path = config.ARTIFACT_PATH
    mtime = Path(path).stat().st_mtime
    if ABI is None or mtime != _artifact_mtime:
        artifact = json.loads(Path(path).read_text())
        ABI = artifact["abi"]
        _artifact_mtime = mtime
    return ABI


def load_abi_and_bytecode() -> Tuple[List[Dict[str, Any]], str]:
    artifact = json.loads(Path(config.ARTIFACT_PATH).read_text())
    return artifact["abi"], artifact["bytecode"]["object"]


def get_contract(w3: Web3, address: str | None = None):
    addr = address or config.CONTRACT_ADDRESS
    if not addr:
        raise RuntimeError("未配置合约地址（设置 CONTRACT_ADDRESS 或部署信息文件）")
    return w3.eth.contract(address=Web3.to_checksum_address(addr), abi=load_abi())


def read_deployment_info(path: Path | None = None) -> dict:
    p = path or config.DEPLOY_INFO
    if not p.exists():
        raise FileNotFoundError(f"缺少部署信息 {p}，请先运行 deploy/deploy.py 或 forge script")
    return json.loads(p.read_text())


def build_batch_tx(w3: Web3, contract_addr: str, items: List[dict],
                   from_addr: str) -> Tuple[Any, int, int]:
    """构造 claimBatch 交易。

    items: store.proofs_for_indices 的输出。
    返回 (tx_params_dict, value_wei, gas_estimate)

    合约在部署时预存发放资金（非 payable 的 claim/claimBatch），
    因此交易的 value 恒为 0，付款从合约余额出。
    """
    c = w3.eth.contract(address=Web3.to_checksum_address(contract_addr), abi=load_abi())
    indices = [int(x["index"]) for x in items]
    accounts = [Web3.to_checksum_address(x["account"]) for x in items]
    amounts = [int(x["amount"]) for x in items]
    proofs = [[bytes.fromhex(p[2:]) for p in x["proof"]] for x in items]

    fn = c.functions.claimBatch(indices, accounts, amounts, proofs)
    value = 0
    tx = fn.build_transaction(
        {
            "from": Web3.to_checksum_address(from_addr),
            "value": value,
            "gas": config.GAS_LIMIT,
            "chainId": w3.eth.chain_id,
            "nonce": w3.eth.get_transaction_count(Web3.to_checksum_address(from_addr)),
        }
    )
    try:
        gas_est = fn.estimate_gas(
            {"from": Web3.to_checksum_address(from_addr), "value": value}
        )
    except Exception:
        # 模拟失败（证明无效/重复/余额不足等）时，返回固定 gas 上限；
        # 真正提交会由链上回滚拒绝。
        gas_est = config.GAS_LIMIT
    return tx, value, gas_est


def send_batch_tx(w3: Web3, tx: dict, private_key: str) -> dict:
    """签名并发送交易，等待收据，返回收据摘要。失败时抛出带 revert 原因的异常。"""
    acct = w3.eth.account.from_key(private_key)
    signed = acct.sign_transaction(tx)
    raw = getattr(signed, "raw_transaction", None) or getattr(signed, "rawTransaction")
    tx_hash = w3.eth.send_raw_transaction(raw)
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=config.TX_TIMEOUT)
    status = receipt.get("status", 0)
    result = {
        "tx_hash": tx_hash.hex() if isinstance(tx_hash, bytes) else str(tx_hash),
        "block_number": receipt["blockNumber"],
        "gas_used": receipt["gasUsed"],
        "status": int(status) if status is not None else 0,
    }
    if result["status"] != 1:
        raise RuntimeError(f"链上交易执行失败（已回滚）：{result}")
    return result
