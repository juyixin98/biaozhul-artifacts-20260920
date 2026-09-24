"""命令行部署：读取 allocations.json，预测地址，构造根，部署并注入资金。

用法（先启动 anvil，再在激活虚拟环境后执行）：
    python -m scripts.deploy                       # 用默认 Anvil 测试密钥
    CONTRACT_ADDRESS=... 不需要；部署结果可通过 /health 查询

部署信息（地址、根）写入 deployment.json，供脚本/测试复用。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import chain as chain_mod  # noqa: E402
from app.allocations import load_allocations  # noqa: E402
from app.config import Settings  # noqa: E402


def main() -> None:
    settings = Settings.from_env()
    w3 = chain_mod.connect(settings.rpc_url)
    allocations = load_allocations(settings.allocations_path)
    total = sum(a.amount_wei for a in allocations)

    contract, address, root, nonce = chain_mod.deploy_claim_contract(
        w3, settings.deployer_private_key, allocations, settings.artifact_path, total
    )

    info = {
        "contract_address": address,
        "merkle_root": "0x" + root.hex(),
        "chain_id": w3.eth.chain_id,
        "funded_wei": str(total),
        "deploy_nonce": nonce,
        "rpc_url": settings.rpc_url,
        "allocations_path": settings.allocations_path,
    }
    out = Path(os.getenv("DEPLOYMENT_FILE", ROOT / "deployment.json"))
    out.write_text(json.dumps(info, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(info, indent=2, ensure_ascii=False))
    print(f"# deployment info written to: {out}", file=sys.stderr)


if __name__ == "__main__":
    main()
