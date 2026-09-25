#!/usr/bin/env python3
"""生成验收用例的证书链与请求样例，输出到 fixtures/ 目录。

用法：
    python scripts/gen_fixtures.py [输出目录]     # 默认 fixtures/

每个用例一个子目录，包含：
    leaf.pem / intermediate-*.pem / root*.pem
    request.json            # 可直接 POST 给 /verify 的请求体
    request-wrong-intermediate.json  (仅 same_name 用例)
"""
from __future__ import annotations

import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from certchain.fixtures import ALL_CASES, DEFAULT_VALIDATION_TIME


def write_pems(case_dir: pathlib.Path, label: str, pem_text: str, index: dict) -> str:
    n = index.get(label, 0)
    index[label] = n + 1
    suffix = "" if n == 0 else f"-{n}"
    filename = f"{label}{suffix}.pem"
    (case_dir / filename).write_text(pem_text, encoding="utf-8")
    return filename


def main() -> None:
    out = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else "fixtures")
    out.mkdir(parents=True, exist_ok=True)
    for case_name, builder in ALL_CASES.items():
        data = builder()
        case_dir = out / case_name
        case_dir.mkdir(parents=True, exist_ok=True)
        index: dict[str, int] = {}

        leaf_file = write_pems(case_dir, "leaf", data["leaf"], index)
        inter_files = [write_pems(case_dir, "intermediate", p, index) for p in data["intermediates"]]
        root_files = [write_pems(case_dir, "root", p, index) for p in data["trust_roots"]]

        request = {
            "leaf": data["leaf"],
            "intermediates": data["intermediates"],
            "trust_roots": data["trust_roots"],
            "validation_time": DEFAULT_VALIDATION_TIME,
            "purpose": "server_tls",
            "hostname": data["hostname"],
        }
        (case_dir / "request.json").write_text(
            json.dumps(request, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        if case_name == "same_name":
            # 同名但密钥不符的中间证书（反向用例）
            wrong = dict(request, intermediates=data["intermediates_wrong_only"])
            (case_dir / "request-wrong-intermediate.json").write_text(
                json.dumps(wrong, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            # 同名异钥的另一个根（演示用，不参与默认请求）
            write_pems(case_dir, "root-other", data["trust_roots_other"][0], index)

        print(f"[{case_name}] -> {case_dir}  (leaf={leaf_file}, "
              f"intermediates={len(inter_files)}, roots={len(root_files)})")


if __name__ == "__main__":
    main()
