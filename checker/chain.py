"""On-chain deployment / upgrade helpers built on web3.py.

Talks ONLY to a local Anvil node over HTTP using the well-known Anvil test
accounts. Used by both the FastAPI service and the integration test / demo
script.
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional

from web3 import Web3
from web3.contract import Contract

ROOT = Path(__file__).resolve().parents[1]
LAYOUTS_DIR = ROOT / "layouts"

# Canonical Anvil deterministic test key #0 (publicly documented, local only).
ANVIL_KEY_0 = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
ERC1967_IMPL_SLOT = (
    0x360894A13BA1A3210667C828492DB98DCA3E2076CC3735A920A3CA505D382BBC
)


class ChainError(RuntimeError):
    pass


@lru_cache(maxsize=1)
def get_web3(rpc_url: str = "http://127.0.0.1:8545") -> Web3:
    w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 20}))
    if not w3.is_connected():
        raise ChainError(f"cannot connect to local chain at {rpc_url}; start anvil first")
    return w3


def load_bundle(name: str) -> Dict[str, Any]:
    p = LAYOUTS_DIR / f"{name}.json"
    if not p.exists():
        raise ChainError(f"unknown contract '{name}' (no layout bundle at {p})")
    return json.loads(p.read_text())


def available_contracts() -> List[str]:
    return sorted(p.stem for p in LAYOUTS_DIR.glob("*.json"))


def _account(w3: Web3, private_key: str):
    acct = w3.eth.account.from_key(private_key)
    if w3.eth.get_balance(acct.address) == 0:
        raise ChainError(f"account {acct.address} has no balance; use a funded Anvil key")
    return acct


def _send(w3: Web3, acct, tx: Dict[str, Any]) -> Dict[str, Any]:
    tx["from"] = acct.address
    tx["nonce"] = w3.eth.get_transaction_count(acct.address)
    tx["gas"] = tx.get("gas") or 3_000_000
    tx["maxFeePerGas"] = w3.to_wei(20, "gwei")
    tx["maxPriorityFeePerGas"] = w3.to_wei(1, "gwei")
    tx["chainId"] = w3.eth.chain_id
    signed = acct.sign_transaction(tx)
    h = w3.eth.send_raw_transaction(signed.raw_transaction)
    rcpt = w3.eth.wait_for_transaction_receipt(h)
    if rcpt.status != 1:
        raise ChainError(f"transaction reverted: {h.hex()}")
    return {"txhash": h.hex(), "receipt": dict(rcpt)}


def deploy_contract(
    w3: Web3,
    acct,
    bundle: Dict[str, Any],
    args: Optional[List[Any]] = None,
) -> Contract:
    contract = w3.eth.contract(abi=bundle["abi"], bytecode=bundle["bytecode"])
    tx = contract.constructor(*(args or [])).build_transaction({"gas": 4_000_000})
    sent = _send(w3, acct, tx)
    rcpt = w3.eth.wait_for_transaction_receipt(sent["txhash"])
    return w3.eth.contract(address=rcpt["contractAddress"], abi=bundle["abi"])


def _encode(w3: Web3, abi: List[Any], fn_name: str, args: List[Any]) -> bytes:
    c = w3.eth.contract(abi=abi)
    return c.encode_abi(fn_name, args)


def deploy_proxy_with(
    w3: Web3,
    acct,
    impl_name: str,
    init_fn: Optional[str] = "initialize",
) -> Dict[str, Any]:
    """Deploy implementation bundle + ERC1967 proxy, calling init via delegatecall."""
    impl_bundle = load_bundle(impl_name)
    impl = deploy_contract(w3, acct, impl_bundle)

    data = b""
    if init_fn:
        data = _encode(w3, impl_bundle["abi"], init_fn, [])

    proxy_bundle = load_bundle("ERC1967Proxy")
    proxy = deploy_contract(w3, acct, proxy_bundle, args=[impl.address, data])
    return {
        "proxy": proxy.address,
        "implementation": impl.address,
        "impl_name": impl_name,
        "proxy_bundle": proxy_bundle,
    }


def read_implementation(w3: Web3, proxy_address: str) -> str:
    raw = w3.eth.get_storage_at(Web3.to_checksum_address(proxy_address), ERC1967_IMPL_SLOT)
    return Web3.to_checksum_address(raw[-20:])


def upgrade_proxy(
    w3: Web3,
    acct,
    proxy_address: str,
    new_impl_name: str,
    proxy_abi: List[Any],
) -> Dict[str, Any]:
    """Deploy new implementation and call upgradeTo(address) through the proxy."""
    bundle = load_bundle(new_impl_name)
    impl = deploy_contract(w3, acct, bundle)
    proxy = w3.eth.contract(
        address=Web3.to_checksum_address(proxy_address), abi=proxy_abi
    )
    tx = proxy.functions.upgradeTo(impl.address).build_transaction({"gas": 1_000_000})
    sent = _send(w3, acct, tx)
    return {
        "new_implementation": impl.address,
        "impl_name": new_impl_name,
        "txhash": sent["txhash"],
    }


# ---------------------------------------------------------------------------
# Demo-data helpers (ABI driven, so they work through any logic version that
# exposes the getters)
# ---------------------------------------------------------------------------

DEMO_GETTERS = [
    "getX",
    "getPacked",
    "getName",
    "getFlags",
    "countLength",
    "getValue",
]


def seed_demo_data(w3: Web3, acct, proxy_address: str, abi: List[Any]) -> Dict[str, Any]:
    """Populate one of every storage type used by V1."""
    c = w3.eth.contract(address=Web3.to_checksum_address(proxy_address), abi=abi)
    calls = [
        ("setX", [123456789]),
        ("setPacked", [0xDEADBEEF, 0xABCD, 0x1234]),
        ("setName", ["hello-upgrade"]),
        ("setFlags", [b"\x01\x02\x03\x04"]),
        ("pushCount", [77]),
        ("pushCount", [88]),
        ("setValue", [5, 555]),
        ("setBaseCounter", [42]),
    ]
    txhashes = []
    for fn, args in calls:
        f = getattr(c.functions, fn)
        tx = f(*args).build_transaction({"gas": 500_000})
        txhashes.append(_send(w3, acct, tx)["txhash"])
    return {"seed_tx": txhashes}


def read_demo_data(w3: Web3, proxy_address: str, abi: List[Any]) -> Dict[str, Any]:
    c = w3.eth.contract(address=Web3.to_checksum_address(proxy_address), abi=abi)

    def try_call(name, *args):
        fn = getattr(c.functions, name, None)
        if fn is None:
            return None
        try:
            return fn(*args).call()
        except Exception:
            return None

    data: Dict[str, Any] = {
        "x": try_call("getX"),
        "packed_y_z_w": _tuple(try_call("getPacked")),
        "name": _maybe_bytes(try_call("getName")),
        "flags": _hexbytes(try_call("getFlags")),
        "counts": _read_counts(c, try_call("countLength")),
        "values[5]": try_call("getValue", 5),
        "baseCounter": try_call("getBaseCounter"),
        "extra": try_call("getExtra"),
        "ab": _tuple(try_call("getAB")),
    }
    data = {k: v for k, v in data.items() if v is not None}
    return data


def _tuple(v):
    return list(v) if isinstance(v, (tuple, list)) else v


def _maybe_bytes(v):
    return v.decode() if isinstance(v, bytes) else v


def _hexbytes(v):
    return v.hex() if isinstance(v, (bytes, bytearray)) else v


def _read_counts(contract, length):
    if length is None:
        return None
    out = []
    for i in range(length):
        try:
            out.append(contract.functions.countAt(i).call())
        except Exception:
            return None
    return out
