"""生成可复现的示例密钥与离线投票文件（真实 Ed25519 签名）。

用法:  python scripts/gen_examples.py
输出:  examples/ 目录下的密钥、链配置和投票 JSON。

密钥由固定种子派生（仅用于测试！），所以示例文件可复现、可直接入库演示。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from app.crypto import derive_pubkey, sign_vote

ROOT = Path(__file__).resolve().parent.parent
EX = ROOT / "examples"

CHAIN_ID = "test-chain-1"
EPOCH_LENGTH = 10
VALIDATORS = [
    ("alice", b"seed:alice:" + b"\x00" * 21),
    ("bob", b"seed:bob::" + b"\x00" * 22),
    ("carol", b"seed:carol" + b"\x00" * 22),
]
# 初始权益（微单位，1_000_000 = 1 代币）
POWER = {"alice": 100_000_000, "bob": 40_000_000, "carol": 10_000_000}


def vote_json(priv: Ed25519PrivateKey, pub: bytes, round: int, block_hash: bytes) -> dict:
    sig = sign_vote(
        priv,
        chain_id=CHAIN_ID,
        validator_pubkey=pub,
        round=round,
        block_hash=block_hash,
    )
    return {
        "chain_id": CHAIN_ID,
        "validator_pubkey": pub.hex(),
        "round": round,
        "block_hash": block_hash.hex(),
        "signature": sig.hex(),
    }


def main() -> None:
    EX.mkdir(exist_ok=True)
    keys = {}
    for name, seed in VALIDATORS:
        priv = Ed25519PrivateKey.from_private_bytes(seed)
        pub = derive_pubkey(priv)
        keys[name] = {
            "priv_hex": seed.hex(),  # 测试私钥（种子即私钥字节）
            "pubkey_hex": pub.hex(),
            "power": POWER[name],
        }

    (EX / "keys.json").write_text(
        json.dumps({"chain_id": CHAIN_ID, "validators": keys}, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    (EX / "chain.json").write_text(
        json.dumps({"chain_id": CHAIN_ID, "epoch_length": EPOCH_LENGTH}, indent=2),
        encoding="utf-8",
    )

    alice_priv = Ed25519PrivateKey.from_private_bytes(VALIDATORS[0][1])
    alice_pub = bytes.fromhex(keys["alice"]["pubkey_hex"])
    bob_priv = Ed25519PrivateKey.from_private_bytes(VALIDATORS[1][1])
    bob_pub = bytes.fromhex(keys["bob"]["pubkey_hex"])

    # --- 场景一：alice 在 round 7（epoch 0）双签两个不同区块 ---
    (EX / "vote_round7_blockA.json").write_text(json.dumps(
        vote_json(alice_priv, alice_pub, 7, b"\xa1" * 32), indent=2), encoding="utf-8")
    (EX / "vote_round7_blockB.json").write_text(json.dumps(
        vote_json(alice_priv, alice_pub, 7, b"\xb2" * 32), indent=2), encoding="utf-8")
    # 与 A 完全相同的重复票（不算双签）
    (EX / "vote_round7_blockA_dup.json").write_text(json.dumps(
        vote_json(alice_priv, alice_pub, 7, b"\xa1" * 32), indent=2), encoding="utf-8")

    # --- 场景二：边界轮次 round 10 属于 epoch 1（引用 epoch 1 快照）---
    (EX / "vote_round10_blockA.json").write_text(json.dumps(
        vote_json(alice_priv, alice_pub, 10, b"\xc3" * 32), indent=2), encoding="utf-8")
    (EX / "vote_round10_blockC.json").write_text(json.dumps(
        vote_json(alice_priv, alice_pub, 10, b"\xd4" * 32), indent=2), encoding="utf-8")

    # --- bob 合法单票 ---
    (EX / "vote_bob_round3.json").write_text(json.dumps(
        vote_json(bob_priv, bob_pub, 3, b"\xe5" * 32), indent=2), encoding="utf-8")

    print(f"示例文件已生成到 {EX}/")
    for p in sorted(EX.glob("*.json")):
        print(" -", p.name)


if __name__ == "__main__":
    main()
