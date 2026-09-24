"""应用配置：从部署清单 deployment.json 读取地址/账户，允许用环境变量覆盖。"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_RPC_URL = "http://127.0.0.1:8545"
ROOT = Path(__file__).resolve().parent.parent


def _default_deployment_path() -> Path:
    return Path(os.environ.get("BOUNDED_DEPLOYMENT_PATH", ROOT / "deployment.json"))


@dataclass(frozen=True)
class Settings:
    rpc_url: str
    chain_id: int
    settlement: str
    token_a: str
    token_b: str
    deployer: str
    maker: str
    taker: str
    fee_receiver: str
    deployer_pk: str
    maker_pk: str
    taker_pk: str
    out_dir: Path

    @classmethod
    def load(cls, deployment_path: Path | None = None) -> "Settings":
        path = deployment_path or _default_deployment_path()
        data: dict[str, Any] = {}
        if path.exists():
            data = json.loads(path.read_text())

        rpc_url = os.environ.get("BOUNDED_RPC_URL", data.get("rpc_url", DEFAULT_RPC_URL))
        chain_id = int(data.get("chain_id", 31337))
        addrs = data.get("addresses", {})
        accts = data.get("accounts", {})

        return cls(
            rpc_url=rpc_url,
            chain_id=chain_id,
            settlement=addrs["settlement"],
            token_a=addrs["tokenA"],
            token_b=addrs["tokenB"],
            deployer=accts["deployer"],
            maker=accts["maker"],
            taker=accts["taker"],
            fee_receiver=accts["feeReceiver"],
            deployer_pk=accts["deployerPrivateKey"],
            maker_pk=accts["makerPrivateKey"],
            taker_pk=accts["takerPrivateKey"],
            out_dir=ROOT / "out",
        )
