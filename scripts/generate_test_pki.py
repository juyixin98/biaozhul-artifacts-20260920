#!/usr/bin/env python3
"""生成示例用本地 PKI 并输出到 examples/pki/ 目录，同时生成 curl 请求样例。

用法:
    python scripts/generate_test_pki.py
"""

from __future__ import annotations

import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from tests.pki_fixtures import TestPKI  # noqa: E402

OUT = pathlib.Path(__file__).resolve().parent.parent / "examples" / "pki"


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    pki = TestPKI()
    files = {
        "root_ca.pem": pki.root_pem,
        "intermediate_ca.pem": pki.inter_pem,
        "leaf.pem": pki.leaf_pem,
        "leaf_expired.pem": pki.expired_leaf_pem,
        "leaf_name_mismatch.pem": pki.mismatch_leaf_pem,
        "non_ca.pem": pki.non_ca_pem,
        "leaf_under_non_ca.pem": pki.leaf_under_non_ca_pem,
        "sub_ca.pem": pki.sub_ca_pem,
        "leaf_under_sub_ca.pem": pki.leaf_under_sub_ca_pem,
        "untrusted_self_signed.pem": pki.untrusted_self_signed_pem,
    }
    for name, content in files.items():
        (OUT / name).write_text(content, encoding="utf-8")

    # 生成一个可直接 POST 的请求样例（正常链）
    sample = {
        "leaf_certificate_pem": pki.leaf_pem,
        "intermediate_certificates_pem": [pki.inter_pem],
        "trust_roots_pem": [pki.root_pem],
        "expected_dns_name": "www.example.com",
    }
    (OUT.parent / "request_valid.json").write_text(
        json.dumps(sample, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"已生成 {len(files)} 个证书文件到 {OUT}")
    print(f"请求样例: {OUT.parent / 'request_valid.json'}")


if __name__ == "__main__":
    main()
