"""FastAPI 只读接口。

所有端点都是 GET，服务不持有任何私钥，也不提供锁定/领取/退款入口——
写操作请用 scripts/ 下的脚本。这样接口被滥用时也无法移动链上资金。
"""

from __future__ import annotations

from fastapi import FastAPI, HTTPException

from .chain import LegClient, load_abi
from .config import (
    ChainConfig, RPC_TIMEOUT, default_configs, load_manifest, DEFAULT_MANIFEST,
)
from .coordinator import assess

try:
    CONFIGS = load_manifest()
except FileNotFoundError:
    CONFIGS = default_configs()

ABI = load_abi()

app = FastAPI(
    title="HTLC 双侧只读协调器",
    description="读取两条本地 Anvil 链上的同一个 HTLC swapId，呈现双侧状态、"
    "时间锁约定与风险边界。不发送任何交易。",
    version="0.1.0",
)


def _client(cfg: ChainConfig) -> LegClient:
    return LegClient(cfg.name, cfg.rpc_url, cfg.chain_id, cfg.address,
                     cfg.deploy_block, ABI, request_timeout=RPC_TIMEOUT)


def _parse_swap_id(raw: str) -> bytes:
    try:
        b = bytes.fromhex(raw.removeprefix("0x"))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="swap_id 必须是 0x 开头的 hex") from exc
    if len(b) != 32:
        raise HTTPException(status_code=400, detail="swap_id 必须是 32 字节")
    return b


@app.get("/health")
def health() -> dict:
    out = {"chains": {}}
    for leg, cfg in CONFIGS.items():
        c = _client(cfg)
        out["chains"][leg] = {
            "rpc_url": cfg.rpc_url,
            "chain_id": cfg.chain_id,
            "contract": cfg.address,
            "reachable": c.reachable(),
            "block_number": (c.w3.eth.block_number if c.reachable() else None),
        }
    out["deployment_manifest"] = DEFAULT_MANIFEST
    return out


@app.get("/swap/{swap_id}")
def swap_status(swap_id: str) -> dict:
    sid = _parse_swap_id(swap_id)
    result = assess(_client(CONFIGS["alpha"]), _client(CONFIGS["beta"]), sid)
    return {
        "swap_id": result.swap_id,
        "status": result.status,
        "time_plan": result.time_plan,
        "risk_flags": result.risk_flags,
        "advice": result.advice,
        "legs": result.legs,
    }


@app.get("/swaps")
def swaps() -> dict:
    """汇总两侧合约上曾经 Locked 的 swapId 及当前状态。"""
    ids: set[str] = set()
    clients = {leg: _client(cfg) for leg, cfg in CONFIGS.items()}
    for c in clients.values():
        if c.contract:
            ids.update(c.list_locked_ids())
    items = []
    for hexid in sorted(ids):
        sid = bytes.fromhex(hexid)
        r = assess(clients["alpha"], clients["beta"], sid)
        items.append({
            "swap_id": r.swap_id,
            "status": r.status,
            "alpha_state": r.legs["alpha"]["state"],
            "beta_state": r.legs["beta"]["state"],
            "risk_flags": r.risk_flags,
        })
    return {"count": len(items), "swaps": items}


@app.get("/config")
def config() -> dict:
    return {
        leg: {
            "rpc_url": c.rpc_url,
            "chain_id": c.chain_id,
            "address": c.address,
            "deploy_block": c.deploy_block,
        }
        for leg, c in CONFIGS.items()
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000)
