#!/usr/bin/env python3
"""生成 README 演示用的样例仓库：合法版本链 + 三个攻击包。

用法::

    python scripts/make_samples.py            # 默认输出到 ./samples
    python scripts/make_samples.py /tmp/samples

目录结构::

    samples/
      1-root/                 初始引导：1.root.json
      2-v1/ 3-v2/ 4-v3/       合法更新（4-v3 同时完成 root 轮换 A -> B）
      attack-rollback/        旧版本整包重放（回滚）
      attack-frozen/          已过期的旧 timestamp 重放（冻结）
      attack-payload-swap/   元数据合法但目标文件被掉包
      manifest.json           各包说明（含攻击包预期错误码）

注意：样例密钥是每次生成时随机新建的，仅用于本地演示，不可用于生产。
"""

from __future__ import annotations

import json
import os
import sys
from datetime import timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import metadata as md  # noqa: E402
from app.repo_tool import RepoKeys, build_release, build_root  # noqa: E402

V1 = {md.TARGETS: 1, md.SNAPSHOT: 1, md.TIMESTAMP: 1}
V2 = {md.TARGETS: 2, md.SNAPSHOT: 2, md.TIMESTAMP: 2}
V3 = {md.TARGETS: 3, md.SNAPSHOT: 3, md.TIMESTAMP: 3}
V4 = {md.TARGETS: 4, md.SNAPSHOT: 4, md.TIMESTAMP: 4}


def _write_release(dirpath: str, release: dict, files: dict[str, bytes],
                   root: bytes | None = None) -> None:
    os.makedirs(dirpath, exist_ok=True)
    if root is not None:
        with open(os.path.join(dirpath, "root.json"), "wb") as fh:
            fh.write(root)
    for role in (md.TIMESTAMP, md.SNAPSHOT, md.TARGETS):
        with open(os.path.join(dirpath, f"{role}.json"), "wb") as fh:
            fh.write(release[role])
    for name, data in files.items():
        with open(os.path.join(dirpath, name), "wb") as fh:
            fh.write(data)


def main(out_dir: str) -> None:
    os.makedirs(out_dir, exist_ok=True)
    keys_a = RepoKeys.generate()
    keys_b = RepoKeys.generate()
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)

    def expiries(ts_days: int = 30) -> dict:
        return {
            md.ROOT: now + timedelta(days=3650),
            md.TARGETS: now + timedelta(days=90),
            md.SNAPSHOT: now + timedelta(days=60),
            md.TIMESTAMP: now + timedelta(days=ts_days),
        }

    # 初始 root（A 密钥集）
    root1 = build_root(1, keys_a, keys_a.root_keys, expiries()[md.ROOT])
    os.makedirs(os.path.join(out_dir, "1-root"), exist_ok=True)
    with open(os.path.join(out_dir, "1-root", "1.root.json"), "wb") as fh:
        fh.write(root1)

    files1 = {"app.txt": b"demo release v1\n"}
    rel1 = build_release(keys_a, V1, files1, expiries())
    _write_release(os.path.join(out_dir, "2-v1"), rel1, files1)

    files2 = {"app.txt": b"demo release v2 - changelog entry\n"}
    rel2 = build_release(keys_a, V2, files2, expiries())
    _write_release(os.path.join(out_dir, "3-v2"), rel2, files2)

    # v3：root 轮换到 B（A+B 双签），三段元数据由 B 签发
    root2 = build_root(
        2,
        keys_b,
        [*keys_a.root_keys, *keys_b.root_keys],
        expiries()[md.ROOT],
    )
    files3 = {"app.txt": b"demo release v3 after root rotation\n"}
    rel3 = build_release(keys_b, V3, files3, expiries())
    _write_release(os.path.join(out_dir, "4-v3-rotated"), rel3, files3, root=root2)

    # B 时代的合法 v2 元数据（用于构造“签名合法但内容有问题”的攻击包）
    rel2_b = build_release(keys_b, V2, files2, expiries())

    # 下面三个攻击包都基于“当前已接受的 B 时代 v3 状态”构造：
    # 签名全部合法（由当前信任的 B 密钥签发），只在版本/过期/内容上做手脚。

    # ---- 攻击包 1：回滚（合法签名的 v2 整包重放，版本号低于当前 v3）----
    _write_release(os.path.join(out_dir, "attack-rollback"), rel2_b, files2)

    # ---- 攻击包 2：冻结（v2 整包重放，且 timestamp 昨天已过期）----
    rel_frozen = build_release(keys_b, V2, files2, {
        **expiries(),
        md.TIMESTAMP: now - timedelta(days=1),
    })
    _write_release(
        os.path.join(out_dir, "attack-frozen"), rel_frozen, files2
    )

    # ---- 攻击包 3：目标文件掉包（B 合法签出全新 v4 元数据，版本检查通过；
    #      但随附文件内容与 targets 声明的 sha256 不符 -> 哈希绑定拒绝）----
    rel4 = build_release(keys_b, V4, files3, expiries())
    _write_release(
        os.path.join(out_dir, "attack-payload-swap"),
        rel4,
        {"app.txt": b"EVIL PAYLOAD - hash does not match\n"},
    )

    manifest = {
        "generated_at": now.isoformat(),
        "steps": [
            {"dir": "1-root", "action": "bootstrap", "expect": "accepted"},
            {"dir": "2-v1", "action": "update", "expect": "accepted"},
            {"dir": "3-v2", "action": "update", "expect": "accepted"},
            {
                "dir": "4-v3-rotated",
                "action": "update (携带 root.json，完成 root v1->v2 轮换)",
                "expect": "accepted",
            },
            {
                "dir": "attack-rollback",
                "action": "update",
                "expect": "rejected: error=rollback（B 合法签名的 v2 重放，版本低于当前 v3）",
            },
            {
                "dir": "attack-frozen",
                "action": "update",
                "expect": "rejected: error=expired（B 合法签名的旧包，timestamp 已过期）",
            },
            {
                "dir": "attack-payload-swap",
                "action": "update",
                "expect": "rejected: error=hash（元数据合法，目标文件 sha256 不符）",
            },
            {
                "dir": "4-v3-rotated",
                "action": "再次提交合法 v3",
                "expect": "rejected: error=rollback（重放当前版本），证明状态未被攻击改变",
            },
        ],
    }
    with open(os.path.join(out_dir, "manifest.json"), "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, ensure_ascii=False, indent=2)

    print(f"样例仓库已生成: {out_dir}")
    for step in manifest["steps"]:
        print(f"  {step['dir']:<24} {step['expect']}")


if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else "samples"
    main(os.path.abspath(out))
