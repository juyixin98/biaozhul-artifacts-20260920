"""FastAPI service exposing the storage-layout checker and local-chain
deployment/upgrade helpers.

Endpoints
---------
GET  /health                     liveness + chain connectivity
GET  /contracts                  layout bundles available for deploy/check
POST /check                      compare two uploaded layout documents
POST /check/named                compare two bundled layouts by name
POST /deploy                     deploy impl + ERC1967 proxy on local Anvil
POST /upgrade                    storage-check, then upgrade a proxy on-chain
GET  /proxy/{address}/state      read demo state through the proxy
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from checker import check_layouts
from checker.chain import (
    ANVIL_KEY_0,
    ChainError,
    available_contracts,
    deploy_proxy_with,
    get_web3,
    load_bundle,
    read_demo_data,
    read_implementation,
    seed_demo_data,
    upgrade_proxy,
)

app = FastAPI(title="upgradeable-storage-checker", version="0.1.0")


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------


class CheckRequest(BaseModel):
    old: Dict[str, Any] = Field(..., description="current layout document")
    new: Dict[str, Any] = Field(..., description="candidate layout document")
    old_contract: Optional[str] = None
    new_contract: Optional[str] = None


class NamedCheckRequest(BaseModel):
    old: str
    new: str


class DeployRequest(BaseModel):
    impl: str = "BoxV1"
    private_key: str = ANVIL_KEY_0
    seed: bool = True


class UpgradeRequest(BaseModel):
    proxy: str
    new_impl: str
    old_impl: Optional[str] = None
    private_key: str = ANVIL_KEY_0
    force: bool = False


# ---------------------------------------------------------------------------
# Basic endpoints
# ---------------------------------------------------------------------------


@app.get("/health")
def health() -> Dict[str, Any]:
    try:
        w3 = get_web3()
        return {"ok": True, "chain": "connected", "chain_id": w3.eth.chain_id}
    except ChainError as e:
        return {"ok": True, "chain": "unavailable", "detail": str(e)}


@app.get("/contracts")
def contracts() -> Dict[str, Any]:
    return {"contracts": available_contracts()}


# ---------------------------------------------------------------------------
# Checker endpoints
# ---------------------------------------------------------------------------


@app.post("/check")
def check(req: CheckRequest) -> Dict[str, Any]:
    try:
        res = check_layouts(
            req.old,
            req.new,
            old_contract=req.old_contract,
            new_contract=req.new_contract,
        )
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    return res.to_dict()


@app.post("/check/named")
def check_named(req: NamedCheckRequest) -> Dict[str, Any]:
    try:
        old = load_bundle(req.old)
        new = load_bundle(req.new)
    except ChainError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return check_layouts(old, new).to_dict()


# ---------------------------------------------------------------------------
# Chain endpoints
# ---------------------------------------------------------------------------


@app.post("/deploy")
def deploy(req: DeployRequest) -> Dict[str, Any]:
    try:
        w3 = get_web3()
        acct = w3.eth.account.from_key(req.private_key)
        out = deploy_proxy_with(w3, acct, req.impl)
        result = {
            "proxy": out["proxy"],
            "implementation": out["implementation"],
            "impl": req.impl,
            "owner": acct.address,
        }
        if req.seed:
            bundle = load_bundle(req.impl)
            seed_demo_data(w3, acct, out["proxy"], bundle["abi"])
            result["seeded"] = True
            result["state"] = read_demo_data(w3, out["proxy"], bundle["abi"])
        return result
    except ChainError as e:
        raise HTTPException(status_code=502, detail=str(e))


@app.post("/upgrade")
def upgrade(req: UpgradeRequest) -> Dict[str, Any]:
    try:
        w3 = get_web3()
        acct = w3.eth.account.from_key(req.private_key)
    except ChainError as e:
        raise HTTPException(status_code=502, detail=str(e))

    # Resolve the current implementation name: prefer the caller's hint,
    # otherwise try to match the on-chain code hash against bundled layouts.
    old_name = req.old_impl
    if old_name is None:
        old_name = _detect_impl_name(w3, req.proxy)
    if old_name is None:
        raise HTTPException(
            status_code=422,
            detail="cannot infer current implementation; pass old_impl explicitly",
        )

    old_bundle = load_bundle(old_name)
    new_bundle = load_bundle(req.new_impl)
    check = check_layouts(old_bundle, new_bundle).to_dict()

    if not check["compatible"] and not req.force:
        return {
            "upgraded": False,
            "blocked": True,
            "reason": "storage layout incompatible",
            "check": check,
        }

    # Snapshot state BEFORE upgrade so we can prove preservation.
    before = read_demo_data(w3, req.proxy, old_bundle["abi"])
    before_impl = read_implementation(w3, req.proxy)

    try:
        up = upgrade_proxy(w3, acct, req.proxy, req.new_impl, old_bundle["abi"])
    except ChainError as e:
        raise HTTPException(status_code=502, detail=str(e))

    after = read_demo_data(w3, req.proxy, new_bundle["abi"])
    after_impl = read_implementation(w3, req.proxy)

    preserved = _preserved(before, after)
    return {
        "upgraded": True,
        "blocked": False,
        "check": check,
        "proxy": req.proxy,
        "old_impl": old_name,
        "new_impl": req.new_impl,
        "implementation_before": before_impl,
        "implementation_after": after_impl,
        "txhash": up["txhash"],
        "state_before": before,
        "state_after": after,
        "state_preserved": preserved,
    }


@app.get("/proxy/{address}/state")
def proxy_state(address: str, impl: Optional[str] = None) -> Dict[str, Any]:
    try:
        w3 = get_web3()
        name = impl or _detect_impl_name(w3, address)
        if name is None:
            raise HTTPException(status_code=422, detail="unknown implementation")
        bundle = load_bundle(name)
        return {
            "proxy": address,
            "implementation": read_implementation(w3, address),
            "impl": name,
            "state": read_demo_data(w3, address, bundle["abi"]),
        }
    except ChainError as e:
        raise HTTPException(status_code=502, detail=str(e))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _detect_impl_name(w3, proxy_address: str) -> Optional[str]:
    """Match the on-chain implementation runtime code against bundled layouts.

    Bundled contracts take no immutable variables and are deployed from the
    exact exported artifact, so deployed bytecode should be identical; we also
    accept a prefix match that excludes the trailing Solidity CBOR metadata.
    """
    try:
        impl_addr = read_implementation(w3, proxy_address)
        code = w3.eth.get_code(impl_addr).hex().removeprefix("0x")
    except Exception:
        return None
    for name in available_contracts():
        if name == "ERC1967Proxy":
            continue
        try:
            bundle = load_bundle(name)
        except ChainError:
            continue
        runtime = bundle.get("deployedBytecode") or ""
        if not runtime:
            continue
        if code == runtime:
            return name
        # Strip trailing CBOR metadata (roughly last 90 bytes) and compare.
        cut = -180
        if len(code) > 200 and code[:cut] == runtime[:cut]:
            return name
    return None


def _preserved(before: Dict[str, Any], after: Dict[str, Any]) -> bool:
    keys = set(before) & set(after)
    return all(before[k] == after[k] for k in keys)
