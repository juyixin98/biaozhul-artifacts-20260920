"""FastAPI 应用：证明生成 + 本地链批量领取。

端点：
  GET  /health                      健康检查（含链连接状态）
  GET  /status                      合约与分配表状态
  POST /allocations                 装载/重建分配表（返回 Merkle 根）
  GET  /allocations                 查看分配表
  GET  /proof/{index}               查询单项证明
  POST /batch/prepare               为索引列表生成批量领取证明与交易数据（不上链）
  POST /batch/submit                生成证明并发送 claimBatch 交易（原子上链）

所有证明均由后端依据内存分配表中的叶子生成；叶子绑定
(chainId, contractAddress, index, account, amount)。
"""
from __future__ import annotations

import logging
from typing import List, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from web3 import Web3

from . import config
from .chain import build_batch_tx, get_contract, get_web3, read_deployment_info
from .merkle import encode_leaf
from .schemas import (
    Allocation,
    PrepareBatchRequest,
    PreparedBatch,
    ProofItem,
    StatusResponse,
    SubmitResponse,
)
from .store import store

logger = logging.getLogger("merkle-claim")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

app = FastAPI(title="Merkle 批量领取证明服务", version="1.0.0")


# ---------------------------------------------------------------------------
# 启动：读取部署信息 + 可选分配表
# ---------------------------------------------------------------------------

@app.on_event("startup")
def _startup() -> None:
    # 解析合约地址：环境变量优先，其次部署信息文件
    if not config.CONTRACT_ADDRESS and config.DEPLOY_INFO.exists():
        info = read_deployment_info()
        config.CONTRACT_ADDRESS = Web3.to_checksum_address(info["address"])
        logger.info("从 %s 载入合约地址 %s", config.DEPLOY_INFO, config.CONTRACT_ADDRESS)
    if config.ALLOCATIONS_FILE and config.CONTRACT_ADDRESS:
        w3 = get_web3()
        chain_id = w3.eth.chain_id
        root = store.load_file(config.ALLOCATIONS_FILE, chain_id, config.CONTRACT_ADDRESS)
        logger.info("已装载分配表 %d 项, root=%s", store.count(), root.hex())


# ---------------------------------------------------------------------------
# 健康 / 状态
# ---------------------------------------------------------------------------

@app.get("/health")
def health() -> dict:
    try:
        w3 = get_web3()
        return {"ok": True, "rpc": config.RPC_URL, "chain_id": w3.eth.chain_id,
                "block": w3.eth.block_number}
    except Exception as e:  # noqa: BLE001
        return JSONResponse(status_code=503, content={"ok": False, "error": str(e)})


@app.get("/status", response_model=StatusResponse)
def status() -> StatusResponse:
    w3 = get_web3()
    addr = config.CONTRACT_ADDRESS or None
    token = owner = "0x0000000000000000000000000000000000000000"
    root_hex: Optional[str] = None
    if addr:
        c = get_contract(w3, addr)
        token = c.functions.token().call()
        owner = c.functions.owner().call()
        root_hex = "0x" + c.functions.merkleRoot().call().hex()
    return StatusResponse(
        rpc_url=config.RPC_URL,
        chain_id=w3.eth.chain_id,
        contract=addr or "",
        token=token,
        owner=owner,
        root=root_hex,
        allocation_count=store.count() if _store_ready() else 0,
    )


def _store_ready() -> bool:
    try:
        store.count()
        return True
    except RuntimeError:
        return False


def _require_contract() -> None:
    if not config.CONTRACT_ADDRESS:
        raise HTTPException(400, "尚未配置合约地址（设置 CONTRACT_ADDRESS 或先运行部署）")


# ---------------------------------------------------------------------------
# 分配表
# ---------------------------------------------------------------------------

@app.post("/allocations")
def set_allocations(allocations: List[Allocation]) -> dict:
    if not config.CONTRACT_ADDRESS:
        raise HTTPException(400, "尚未配置/部署合约，无法生成绑定合约地址的叶子")
    w3 = get_web3()
    data = [a.model_dump() for a in allocations]
    root = store.load(data, w3.eth.chain_id, config.CONTRACT_ADDRESS)
    onchain_root = get_contract(w3).functions.merkleRoot().call()
    match = onchain_root == root
    return {
        "root": "0x" + root.hex(),
        "count": store.count(),
        "chain_id": w3.eth.chain_id,
        "contract": config.CONTRACT_ADDRESS,
        "matches_onchain_root": match,
        "warning": None if match else "后端树根与链上 merkleRoot 不一致，证明将被链上拒绝",
    }


@app.get("/allocations")
def get_allocations() -> dict:
    return {"allocations": store.list_allocations(), "count": store.count()}


# ---------------------------------------------------------------------------
# 证明
# ---------------------------------------------------------------------------

@app.get("/proof/{index}", response_model=ProofItem)
def get_proof(index: int) -> ProofItem:
    try:
        return ProofItem(**store.proof_for_index(index))
    except KeyError:
        raise HTTPException(404, f"索引 {index} 不在分配表中")
    except RuntimeError as e:
        raise HTTPException(400, str(e))


@app.post("/batch/prepare", response_model=PreparedBatch)
def prepare_batch(req: PrepareBatchRequest) -> PreparedBatch:
    _require_contract()
    indices = req.indices
    try:
        items = store.proofs_for_indices(indices)
    except KeyError as e:
        raise HTTPException(404, f"索引 {e} 不在分配表中")
    except (ValueError, RuntimeError) as e:
        raise HTTPException(400, str(e))

    w3 = get_web3()
    sender = w3.eth.account.from_key(config.PRIVATE_KEY).address
    # build_batch_tx 内部对 estimateGas 做了兜底（证明无效等情况下返回固定 gas 上限）
    tx, value, gas_est = build_batch_tx(w3, config.CONTRACT_ADDRESS, items, sender)

    raw_data = tx["data"]
    data_hex = raw_data.hex() if isinstance(raw_data, (bytes, bytearray)) else str(raw_data)
    if not data_hex.startswith("0x"):
        data_hex = "0x" + data_hex

    return PreparedBatch(
        contract=config.CONTRACT_ADDRESS,
        chain_id=w3.eth.chain_id,
        root="0x" + store.root.hex(),
        to=Web3.to_checksum_address(tx["to"]),
        data=data_hex,
        value=tx.get("value", 0),
        gas_estimate=gas_est,
        items=[ProofItem(**i) for i in items],
    )


@app.post("/batch/submit", response_model=SubmitResponse)
def submit_batch(req: PrepareBatchRequest) -> SubmitResponse:
    """原子批量领取：链上 claimBatch 在任一单项无效时整体回滚。"""
    _require_contract()
    indices = req.indices
    try:
        items = store.proofs_for_indices(indices)
    except KeyError as e:
        raise HTTPException(404, f"索引 {e} 不在分配表中")
    except (ValueError, RuntimeError) as e:
        raise HTTPException(400, str(e))

    w3 = get_web3()
    sender = w3.eth.account.from_key(config.PRIVATE_KEY).address
    tx, _value, _gas = build_batch_tx(w3, config.CONTRACT_ADDRESS, items, sender)

    # 先 eth_call 模拟：失败则直接返回可读的 revert 原因，不上链浪费 gas
    try:
        w3.eth.call(tx)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"链上预检失败（交易将回滚，未发送）：{_fmt_revert(e)}")

    try:
        from .chain import send_batch_tx
        receipt = send_batch_tx(w3, tx, config.PRIVATE_KEY)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"交易被链上拒绝/回滚：{e}")
    return SubmitResponse(**receipt, items=[ProofItem(**i) for i in items])


def _fmt_revert(e: Exception) -> str:
    """尽量提取可读的 revert 原因。"""
    msg = str(e)
    data = getattr(e, "data", None)
    if isinstance(data, str) and data.startswith("0x"):
        # 尝试解码已知错误选择器
        from .chain import load_abi
        try:
            for entry in load_abi():
                if entry.get("type") == "error":
                    import eth_utils
                    sig = entry["name"] + "(" + ",".join(i["type"] for i in entry.get("inputs", [])) + ")"
                    sel = eth_utils.keccak(text=sig)[:4].hex()
                    if data[2:10] == sel:
                        return f"合约 revert: {entry['name']} (data={data[:74]}...)"
        except Exception:  # noqa: BLE001
            pass
        return f"合约 revert (data={data[:74]}...)"
    return msg


# ---------------------------------------------------------------------------
# 便捷：只读校验某叶子是否会被合约接受（eth_call，不发交易）
# ---------------------------------------------------------------------------

@app.get("/verify/{index}")
def verify_index(index: int) -> dict:
    item = store.proof_for_index(index)
    w3 = get_web3()
    c = get_contract(w3)
    claimed = c.functions.isClaimed(index).call()
    local_leaf = encode_leaf(w3.eth.chain_id, config.CONTRACT_ADDRESS,
                             item["index"], item["account"], item["amount"])
    onchain_leaf = c.functions.leafHash(item["index"], item["account"], item["amount"]).call()
    onchain_hex = "0x" + bytes(onchain_leaf).hex()
    return {
        "index": index,
        "claimed": claimed,
        "local_leaf": "0x" + local_leaf.hex(),
        "onchain_leaf": onchain_hex,
        "leaf_matches": local_leaf.hex() == bytes(onchain_leaf).hex(),
    }
