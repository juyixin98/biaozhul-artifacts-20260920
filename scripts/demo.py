#!/usr/bin/env python3
"""端到端演示：覆盖别名、未知字段、数组、嵌套旁路、策略竞争与可核验记录。

运行：
    PYTHONPATH=src python3 scripts/demo.py

输出写入 demo_out/（不影响仓库示例数据）。
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from mde.crypto import generate_fernet_key  # noqa: E402
from mde.errors import PolicyConflict, VerificationError  # noqa: E402
from mde.policy import FilePolicyStore  # noqa: E402
from mde.service import ExportService  # noqa: E402
from mde.crypto import generate_signing_key, save_private_key  # noqa: E402

OUT = os.path.join(os.path.dirname(__file__), "..", "demo_out")


def jdump(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2)


def main() -> int:
    os.makedirs(OUT, exist_ok=True)
    store = FilePolicyStore(os.path.join(OUT, "policies"))
    key = generate_signing_key()
    save_private_key(key, os.path.join(OUT, "test_signing_key.pem"))
    svc = ExportService(store, signing_private_key=key,
                        bundle_dir=os.path.join(OUT, "bundles"))

    print("=" * 70)
    print("步骤 1：发布策略 v1（默认 deny；含别名、数组、嵌套 deny）")
    print("=" * 70)
    with open(os.path.join(os.path.dirname(__file__), "..", "examples",
                           "policy_hr.json"), encoding="utf-8") as f:
        policy_doc = json.load(f)
    p1 = store.publish(
        "hr-export", policy_doc["rules"], aliases=policy_doc["aliases"],
        default_action=policy_doc["default_action"],
        description=policy_doc["description"])
    print(f"已发布 hr-export v{p1.version}，fingerprint={p1.fingerprint[:24]}…")

    with open(os.path.join(os.path.dirname(__file__), "..", "examples",
                           "record.json"), encoding="utf-8") as f:
        data = json.load(f)

    print("\n" + "=" * 70)
    print("步骤 2：按用途 analytics 导出（固定 v1）")
    print("=" * 70)
    bundle = svc.export(data, "hr-export", "analytics", version=1)
    print("输出（注意 ssn/salary/medical/未知字段均不在其中）：")
    print(jdump(bundle["output"]))
    print("\n统计：", jdump(bundle["manifest"]["stats"]))

    print("\n" + "=" * 70)
    print("步骤 3：逐字段决策记录（节选）")
    print("=" * 70)
    for d in bundle["decisions"]:
        extra = ""
        if "input_path" in d:
            extra = f"  input={d['input_path']} (别名)"
        if d["decision"] != "passthrough":
            print(f"  {d['decision']:<10} {d['path']}  by={d['by']}{extra}")

    print("\n" + "=" * 70)
    print("步骤 4：核验导出包（签名 + 摘要 + 用原始数据全量重算）")
    print("=" * 70)
    report = svc.verify_bundle(bundle, source_data=data)
    for c in report["checks"]:
        print(f"  [{'PASS' if c['ok'] else 'FAIL'}] {c['check']}: {c['detail']}")

    print("\n" + "=" * 70)
    print("步骤 5：篡改检测（改一个输出值）")
    print("=" * 70)
    tampered = json.loads(json.dumps(bundle))
    tampered["output"]["full_name"] = "Attacker"
    try:
        svc.verify_bundle(tampered)
        print("  未检测到篡改（异常！）")
    except VerificationError as exc:
        print(f"  已拦截：{exc}")

    print("\n" + "=" * 70)
    print("步骤 6：策略更新竞争（乐观锁）")
    print("=" * 70)
    p2 = store.publish(
        "hr-export",
        [{"path": "employee_id", "action": "allow"},
         {"path": "full_name", "action": "allow"},
         {"path": "email", "action": "deny"}],
        aliases=policy_doc["aliases"],
        expected_version=1,
        description="v2: 邮箱改为直接拒绝")
    print(f"  基于 v1 的更新成功 -> v{p2.version}")
    try:
        store.publish("hr-export",
                      [{"path": "employee_id", "action": "allow"}],
                      expected_version=1)
        print("  过期更新竟然成功（异常！）")
    except PolicyConflict as exc:
        print(f"  过期更新被拒绝：{exc}")

    print("\n  已导出的旧包仍固定在 v1（复算仍一致）：")
    old_report = svc.verify_bundle(bundle, source_data=data)
    print(f"  旧包策略版本={old_report['policy']['version']}，核验={old_report['ok']}")
    new_bundle = svc.export(data, "hr-export", "analytics")
    print(f"  新导出使用 v{new_bundle['manifest']['policy']['version']}，"
          f"输出中是否有邮箱字段: {'mail' in new_bundle['output']}")

    print("\n" + "=" * 70)
    print("步骤 7：Fernet 信封加密导出")
    print("=" * 70)
    enc_key = generate_fernet_key()
    enc = svc.export(data, "hr-export", "analytics", encryption_key=enc_key)
    outer = json.dumps(enc, ensure_ascii=False)
    print(f"  加密包 schema={enc['schema_version']}")
    print(f"  外层是否含姓名明文: {'Zhang San' in outer}")
    enc_report = svc.verify_bundle(enc, encryption_key=enc_key, source_data=data)
    print(f"  持钥解密+核验: {enc_report['ok']}")
    try:
        svc.verify_bundle(enc, encryption_key=generate_fernet_key())
    except VerificationError as exc:
        print(f"  错误密钥被拒绝: {exc}")

    print("\n声明：", bundle["manifest"]["disclaimer"])
    print("\n产物已写入:", os.path.abspath(OUT))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
