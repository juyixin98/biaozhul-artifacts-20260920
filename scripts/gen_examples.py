"""生成 examples/ 下的示例输入（确定性密钥 + 真实 Ed25519 签名）。

用法：.venv/bin/python scripts/gen_examples.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import crypto  # noqa: E402

OUT = Path(__file__).resolve().parent.parent / "examples"

BLOCK_A = "aa" * 32
BLOCK_B = "bb" * 32


def validators(spec):
    out, keys = [], {}
    for vid, weight in spec:
        sk = crypto.derive_private_key(f"demo-validator:{vid}".encode())
        out.append(
            {"id": vid, "weight": weight, "public_key": crypto.public_key_hex(sk)}
        )
        keys[vid] = sk
    return out, keys


def vote(keys, epoch, vid, block):
    return {
        "epoch": epoch,
        "validator_id": vid,
        "block_hash": block,
        "signature": crypto.sign_vote(keys[vid], epoch, block),
    }


def dump(name, obj):
    (OUT / name).write_text(json.dumps(obj, indent=2, ensure_ascii=False) + "\n")
    print(f"写入 examples/{name}")


def main():
    OUT.mkdir(exist_ok=True)

    # 场景 1：正常终局（3 名验证者各权重 1，3 票 > 2/3 终局）
    vals, keys = validators([("alice", 1), ("bob", 1), ("carol", 1)])
    dump("epoch1.json", {"epoch": 1, "validators": vals})
    dump("votes_epoch1.json", [vote(keys, 1, v, BLOCK_A) for v in ("alice", "bob", "carol")])

    # 场景 2：终局冲突（4 名验证者；a,b,c 投 A 终局，a,b 双投 B，d 投 B → 冻结）
    vals2, keys2 = validators([("n1", 1), ("n2", 1), ("n3", 1), ("n4", 1)])
    dump("epoch2.json", {"epoch": 2, "validators": vals2})
    dump(
        "votes_epoch2_conflict.json",
        [
            vote(keys2, 2, "n1", BLOCK_A),
            vote(keys2, 2, "n2", BLOCK_A),
            vote(keys2, 2, "n3", BLOCK_A),
            vote(keys2, 2, "n1", BLOCK_B),  # n1 双投
            vote(keys2, 2, "n2", BLOCK_B),  # n2 双投
            vote(keys2, 2, "n4", BLOCK_B),
        ],
    )

    # 场景 3：加权 + 零权重（10/10/0，总 20，需 >=14；零权重票合法但不计权）
    vals3, keys3 = validators([("whale1", 10), ("whale2", 10), ("observer", 0)])
    dump("epoch3.json", {"epoch": 3, "validators": vals3})
    dump(
        "votes_epoch3.json",
        [
            vote(keys3, 3, "observer", BLOCK_A),  # 零权重：接收但不计权
            vote(keys3, 3, "whale1", BLOCK_A),    # 10 < 14，不终局
            vote(keys3, 3, "whale2", BLOCK_A),    # 20 >= 14，终局
        ],
    )


if __name__ == "__main__":
    main()
