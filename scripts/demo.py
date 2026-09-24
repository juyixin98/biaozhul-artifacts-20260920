#!/usr/bin/env python3
"""端到端示例：直接通过 web3.py 在本机 Anvil 上演示完整存取款流程。

不经过 HTTP，用于快速验证链上行为；HTTP 版本见 backend/tests 与 README。

流程：
  1. 连接 Anvil、读取 deployments/local.json；
  2. 用两个 anvil 测试账户铸造资产；
  3. Alice 首存（触发 1000 死份额锁定）；
  4. Bob 极小存款尝试（0 份额会被回滚）；
  5. Bob 正常存款后全额赎回；
  6. 打印每一步的整数会计数据。
"""
from __future__ import annotations

import sys
from pathlib import Path

# 允许直接 `python scripts/demo.py` 运行。
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.app import chain as chainlib  # noqa: E402
from backend.app.config import ANVIL_TEST_PRIVATE_KEYS, RPC_URL  # noqa: E402

WAD = 10**18


def log(title: str, **fields: object) -> None:
    print(f"\n=== {title} ===")
    for key, value in fields.items():
        print(f"  {key}: {value}")


def main() -> int:
    w3 = chainlib.connect(RPC_URL)
    b = chainlib.load_bundle(RPC_URL)
    alice = w3.eth.account.from_key(ANVIL_TEST_PRIVATE_KEYS[0])
    bob = w3.eth.account.from_key(ANVIL_TEST_PRIVATE_KEYS[1])
    print(f"RPC        : {RPC_URL}")
    print(f"chain id   : {b.chain_id}")
    print(f"token      : {b.token_address}")
    print(f"vault      : {b.vault_address}")
    print(f"alice      : {alice.address}")
    print(f"bob        : {bob.address}")

    def send(acct, func):
        return chainlib._send(w3, acct, func)

    # --- 水龙头 -------------------------------------------------------------
    # Alice 需要：5000 枚首存 + 约 500 万枚捐赠。
    send(alice, b.token.functions.mint(5_010_000 * WAD))
    send(bob, b.token.functions.mint(10_000 * WAD))
    send(alice, b.token.functions.approve(b.vault_address, 2**256 - 1))
    send(bob, b.token.functions.approve(b.vault_address, 2**256 - 1))

    # --- Alice 首存 ----------------------------------------------------------
    first_assets = 5_000 * WAD
    preview = b.vault.functions.previewDeposit(first_assets).call()
    rcpt = send(alice, b.vault.functions.deposit(first_assets, alice.address, 0))
    minted = b.vault.functions.balanceOf(alice.address).call()
    log(
        "Alice 首次存款（空库初始化）",
        assets=first_assets,
        preview_shares=preview,
        minted_shares=minted,
        dead_shares=b.vault.functions.balanceOf("0x0000000000000000000000000000000000000001").call(),
        total_supply=b.vault.functions.totalSupply().call(),
        total_assets=b.vault.functions.totalAssets().call(),
        tx=rcpt.transactionHash.hex(),
    )

    # --- Bob 极小存款：抬汇率后必然回滚 ZeroShares ---------------------------
    # Alice 直接向金库捐赠，把汇率抬高约 1000 倍（经典首发捐赠场景）。
    donation = 5_000 * WAD * 999
    send(alice, b.token.functions.transfer(b.vault_address, donation))
    rate = b.vault.functions.totalAssets().call() // b.vault.functions.totalSupply().call()
    tiny = max(rate - 1, 1)
    try:
        send(bob, b.vault.functions.deposit(tiny, bob.address, 0))
        print("\n意外：极小存款没有回滚！")
        return 1
    except chainlib.ChainError as exc:
        log("Bob 极小存款被拒（向下取整为 0 份额）", assets=tiny, revert=str(exc)[:160])

    # --- Bob 正常存款 --------------------------------------------------------
    bob_assets = 1_234 * WAD + 7  # 故意带零头，观察向下取整
    bob_preview = b.vault.functions.previewDeposit(bob_assets).call()
    min_shares = chainlib.apply_slippage_floor(bob_preview, 50)
    send(bob, b.vault.functions.deposit(bob_assets, bob.address, min_shares))
    bob_shares = b.vault.functions.balanceOf(bob.address).call()
    log(
        "Bob 存款（含 7 wei 零头，0.5% 滑点）",
        assets=bob_assets,
        preview_shares=bob_preview,
        actual_shares=bob_shares,
        min_shares=min_shares,
    )

    # --- Bob 全额赎回 --------------------------------------------------------
    out_preview = b.vault.functions.previewRedeem(bob_shares).call()
    bal_before = b.token.functions.balanceOf(bob.address).call()
    send(bob, b.vault.functions.redeem(bob_shares, bob.address, bob.address, 0))
    bal_after = b.token.functions.balanceOf(bob.address).call()
    log(
        "Bob 全额赎回",
        burned_shares=bob_shares,
        preview_assets=out_preview,
        received_assets=bal_after - bal_before,
        vault_total_assets=b.vault.functions.totalAssets().call(),
        vault_total_shares=b.vault.functions.totalSupply().call(),
    )

    print("\n示例完成。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
