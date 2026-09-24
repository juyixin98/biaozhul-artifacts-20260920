"""生成一个可直接用 curl 上传的演示“更新仓库”。

用法::

    .venv/bin/python scripts/make_demo_repo.py [输出目录，默认 ./demo]

产物：

* ``bootstrap_root_public.txt`` —— 带外引导 root 公钥（hex），启动服务端时
  通过环境变量 ``BOOTSTRAP_ROOT_PUBLIC`` 注入；
* ``v1/``、``v2/``、``v3/`` —— 三个合法版本的元数据与目标文件；
* ``attacks/`` —— 三类攻击包（混搭历史文件、冻结旧时间戳、非法根轮换、
  目标文件篡改）；
* ``curl-examples.sh`` —— 可直接执行的请求样例。
"""

from __future__ import annotations

import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from app.builder import Repository, RoleKeys, bump_all
from app import crypto_utils


def _write_role(out: pathlib.Path, name: str, raw: bytes) -> None:
    (out / f"{name}.json").write_bytes(raw)


def _write_bundle(out: pathlib.Path, bundle, *, root: bool = True) -> None:
    out.mkdir(parents=True, exist_ok=True)
    _write_role(out, "timestamp", bundle.timestamp)
    _write_role(out, "snapshot", bundle.snapshot)
    _write_role(out, "targets", bundle.targets)
    if root and bundle.root is not None:
        _write_role(out, "root", bundle.root)
    files_dir = out / "files"
    files_dir.mkdir(exist_ok=True)
    for name, data in bundle.files.items():
        (files_dir / name).write_bytes(data)


def main(out_dir: str = "demo") -> None:
    out = pathlib.Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    repo = Repository.create()
    bootstrap = repo.root_pub_hex()

    # ---- v1 ---------------------------------------------------------------
    v1_bundle = repo.bundle()
    _write_bundle(out / "v1", v1_bundle)

    # ---- v2：合法的下一版本 ----------------------------------------------
    bump_all(repo, {"app.bin": b"app-version-2", "notes.txt": b"release notes v2\n"})
    v2_bundle = repo.bundle(include_root=False)
    _write_bundle(out / "v2", v2_bundle, root=False)

    # ---- 攻击包（在对应客户端状态下生成，确保命中预期防线） ----------------

    # 1) 混搭历史文件：当前时间戳 + v1 的 snapshot/targets/文件。
    #    对已装 v2 的客户端：timestamp 绑定 v2 snapshot 哈希 -> 哈希链断裂；
    #    即使绕过绑定，snapshot 版本 1 < 已信任 2 -> 回滚。
    attack_mix = repo.mix_historical(v1_bundle)

    # 2) 冻结重放：对已装 v2 的客户端重放完整的旧版 v1 包。
    #    旧签名当时全部合法，但 timestamp 版本 1 < 已信任 2 -> TIMESTAMP_ROLLBACK。
    attack_frozen = v1_bundle

    # 3) 目标文件篡改（等长，直接命中哈希校验）
    evil = b"X" * len(repo.files["app.bin"])
    attack_tamper = repo.tamper_file("app.bin", evil)

    # ---- 合法根轮换：v1 root 密钥签名 root v2，切换全套新密钥 -------------
    new_roles = {r: RoleKeys([crypto_utils.generate_keypair()[0]])
                 for r in ("root", "targets", "snapshot", "timestamp")}
    repo.rotate_root(new_roles)
    v3_bundle = repo.bundle()
    _write_bundle(out / "v3", v3_bundle)

    # 4) 非法根轮换：攻击者自制全套密钥并自签同版本 root v2（未经 root v1 授权）
    attacker = {r: RoleKeys([crypto_utils.generate_keypair()[0]])
                for r in ("root", "targets", "snapshot", "timestamp")}
    forged = repo.build_root_envelope(attacker, 2, attacker["root"].priv_hexes)
    attack_forged_root = repo.bundle(override={"root": forged})

    _write_bundle(out / "attacks/01-mix-historical", attack_mix, root=False)
    _write_bundle(out / "attacks/02-frozen-timestamp", attack_frozen)
    _write_bundle(out / "attacks/04-tampered-target", attack_tamper, root=False)
    _write_bundle(out / "attacks/03-forged-root", attack_forged_root)

    (out / "bootstrap_root_public.txt").write_text(bootstrap + "\n", encoding="utf-8")

    # ---- curl 样例 --------------------------------------------------------
    curl = f"""#!/usr/bin/env bash
# 由 scripts/make_demo_repo.py 生成。假设服务监听 127.0.0.1:8000。
# 所有“应被拒绝”的请求只打印错误体、不退出脚本（curl 使用 || true）。
BASE=${{BASE:-http://127.0.0.1:8000}}
D={out_dir}

show() {{ python3 -m json.tool 2>/dev/null || cat; }}

echo '== 健康检查（初始状态） =='
curl -sS "$BASE/health" | show

echo '== 安装 v1（引导，必须携带 root） =='
curl -sS -X POST "$BASE/updates" \\
  -F "root=@$D/v1/root.json;type=application/json" \\
  -F "timestamp=@$D/v1/timestamp.json;type=application/json" \\
  -F "snapshot=@$D/v1/snapshot.json;type=application/json" \\
  -F "targets=@$D/v1/targets.json;type=application/json" \\
  -F "files=@$D/v1/files/app.bin;filename=app.bin" | show

echo '== 下载已验证目标 =='
curl -sS "$BASE/targets/app.bin"; echo

echo '== 更新到 v2（不带 root） =='
curl -sS -X POST "$BASE/updates" \\
  -F "timestamp=@$D/v2/timestamp.json;type=application/json" \\
  -F "snapshot=@$D/v2/snapshot.json;type=application/json" \\
  -F "targets=@$D/v2/targets.json;type=application/json" \\
  -F "files=@$D/v2/files/app.bin;filename=app.bin" \\
  -F "files=@$D/v2/files/notes.txt;filename=notes.txt" | show

echo '== 攻击 1：混搭历史文件（预期 HASH_MISMATCH / 哈希链断裂） =='
curl -sS -X POST "$BASE/updates" \\
  -F "timestamp=@$D/attacks/01-mix-historical/timestamp.json;type=application/json" \\
  -F "snapshot=@$D/attacks/01-mix-historical/snapshot.json;type=application/json" \\
  -F "targets=@$D/attacks/01-mix-historical/targets.json;type=application/json" \\
  -F "files=@$D/attacks/01-mix-historical/files/app.bin;filename=app.bin" || true
echo

echo '== 攻击 2：冻结重放旧版 v1（预期 TIMESTAMP_ROLLBACK） =='
curl -sS -X POST "$BASE/updates" \\
  -F "root=@$D/attacks/02-frozen-timestamp/root.json;type=application/json" \\
  -F "timestamp=@$D/attacks/02-frozen-timestamp/timestamp.json;type=application/json" \\
  -F "snapshot=@$D/attacks/02-frozen-timestamp/snapshot.json;type=application/json" \\
  -F "targets=@$D/attacks/02-frozen-timestamp/targets.json;type=application/json" \\
  -F "files=@$D/attacks/02-frozen-timestamp/files/app.bin;filename=app.bin" || true
echo

echo '== 攻击 4：篡改目标文件（预期 TARGET_HASH_MISMATCH） =='
curl -sS -X POST "$BASE/updates" \\
  -F "timestamp=@$D/attacks/04-tampered-target/timestamp.json;type=application/json" \\
  -F "snapshot=@$D/attacks/04-tampered-target/snapshot.json;type=application/json" \\
  -F "targets=@$D/attacks/04-tampered-target/targets.json;type=application/json" \\
  -F "files=@$D/attacks/04-tampered-target/files/app.bin;filename=app.bin" \\
  -F "files=@$D/attacks/04-tampered-target/files/notes.txt;filename=notes.txt" || true
echo

echo '== 攻击 3：伪造根轮换（预期 THRESHOLD_NOT_MET / BAD_SIGNATURE） =='
curl -sS -X POST "$BASE/updates" \\
  -F "root=@$D/attacks/03-forged-root/root.json;type=application/json" \\
  -F "timestamp=@$D/attacks/03-forged-root/timestamp.json;type=application/json" \\
  -F "snapshot=@$D/attacks/03-forged-root/snapshot.json;type=application/json" \\
  -F "targets=@$D/attacks/03-forged-root/targets.json;type=application/json" \\
  -F "files=@$D/attacks/03-forged-root/files/app.bin;filename=app.bin" \\
  -F "files=@$D/attacks/03-forged-root/files/notes.txt;filename=notes.txt" || true
echo

echo '== 合法 v3：经授权的 root 轮换，从合法下一版本恢复 =='
curl -sS -X POST "$BASE/updates" \\
  -F "root=@$D/v3/root.json;type=application/json" \\
  -F "timestamp=@$D/v3/timestamp.json;type=application/json" \\
  -F "snapshot=@$D/v3/snapshot.json;type=application/json" \\
  -F "targets=@$D/v3/targets.json;type=application/json" \\
  -F "files=@$D/v3/files/app.bin;filename=app.bin" \\
  -F "files=@$D/v3/files/notes.txt;filename=notes.txt" | show

echo '== 最终信任状态 =='
curl -sS "$BASE/health" | show
"""
    script = out / "curl-examples.sh"
    script.write_text(curl, encoding="utf-8")
    script.chmod(0o755)

    print(f"演示仓库已生成于 {out.resolve()}")
    print(f"引导 root 公钥: {bootstrap}")
    print(f"启动服务端: BOOTSTRAP_ROOT_PUBLIC=$(cat {out}/bootstrap_root_public.txt) "
          f"DATA_DIR=./data .venv/bin/uvicorn app.main:app --port 8000")
    print(f"请求样例: bash {out}/curl-examples.sh")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "demo")
