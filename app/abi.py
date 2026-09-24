from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent


def load_abi(contract_name: str) -> list[dict[str, Any]]:
    """Load a contract ABI produced by `forge build` from out/."""
    artifact = REPO_ROOT / "out" / f"{contract_name}.sol" / f"{contract_name}.json"
    if not artifact.exists():
        raise FileNotFoundError(
            f"Missing artifact {artifact}. Run `forge build` first."
        )
    with artifact.open() as fh:
        return json.load(fh)["abi"]


@lru_cache(maxsize=1)
def settlement_abi() -> list[dict[str, Any]]:
    return load_abi("BoundedOrderSettlement")


@lru_cache(maxsize=1)
def erc20_abi() -> list[dict[str, Any]]:
    return load_abi("MockERC20")
