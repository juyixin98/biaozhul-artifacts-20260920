"""导出任务：组装带签名的导出包，并支持离线复验。

导出包（mde/export-package@v1）是自描述的：内嵌策略快照、输出、逐字段决策记录
与 Ed25519 签名。复验方持有公钥即可核验：

1. 签名（包体未被篡改）
2. 内嵌策略快照与包中声明的策略指纹一致
3. 输出与决策记录逐字段一致（present 的输出摘要可重算）

若复验方同时持有原始记录与 transform_secret（本地测试场景），还可重放引擎，
比对输出与决策完全一致（决策记录中仅时间无关字段参与比对）。
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import OUTPUT_SCHEMA, PACKAGE_FORMAT
from .engine import export_records
from .keys import LocalKeys
from .policy import CompiledPolicy, PolicyStore, StoredPolicy, to_plain
from .signing import fingerprint, sha256_hex, canonical, verify as sig_verify
from .transforms import TransformContext

# 固定随包声明：明确不声称匿名化
DISCLAIMERS = [
    "这是字段级最小披露导出，不是匿名化；输出仍可能被关联再识别。",
    "pseudonymize 使用目的绑定 HMAC-SHA256 假名，不抗跨数据集关联攻击。",
    "决策记录本身含字段存在性与输入摘要，应按敏感数据同等保护。",
    "泛化不改变数据控制者的合规义务；用途绑定由策略与审计流程保证，密码学不强制。",
]

def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class ExportService:
    store: PolicyStore
    keys: LocalKeys

    def _ctx(self, compiled: CompiledPolicy, purpose: str) -> TransformContext:
        return TransformContext(
            transform_secret=self.keys.transform_secret,
            policy_fingerprint=compiled.policy_fingerprint,
            purpose=purpose,
        )

    def export(
        self,
        *,
        records: List[dict],
        purpose: str,
        policy_fingerprint: str,
        task_id: Optional[str] = None,
        created_at: Optional[str] = None,
        requester: str = "local-test",
    ) -> dict:
        """按固定的策略指纹快照导出（发布新版本不影响本任务）。"""
        stored: StoredPolicy = self.store.get(policy_fingerprint)
        compiled = stored.policy
        ctx = self._ctx(compiled, purpose)
        outputs, decisions = export_records(records, compiled, purpose, ctx)

        task_id = task_id or f"task-{uuid.uuid4().hex}"
        created_at = created_at or utc_now_iso()
        doc = compiled.doc
        body = {
            "task": {
                "task_id": task_id,
                "purpose": purpose,
                "requester": requester,
                "created_at": created_at,
                "policy_id": doc["policy_id"],
                "policy_revision": doc["revision"],
                "policy_fingerprint": compiled.policy_fingerprint,
                "policy_created_at": stored.created_at,
                "key_id": self.keys.key_id,
            },
            "manifest": {
                "package_format": PACKAGE_FORMAT,
                "output_schema": OUTPUT_SCHEMA,
                "record_count": len(records),
                "decision_count": len(decisions),
                "records_digest": sha256_hex(canonical(records)),
                "output_digest": sha256_hex(canonical(outputs)),
                "decisions_digest": sha256_hex(canonical(decisions)),
            },
            "policy_snapshot": to_plain(doc),
            "output": outputs,
            "decisions": decisions,
            "disclaimers": DISCLAIMERS,
        }
        signature = _sign_body(self.keys, body)
        return {**body, "signature": signature}

    def replay(
        self, *, records: List[dict], package: dict
    ) -> Tuple[List[Any], List[dict]]:
        """用包内策略快照重放引擎（用于持有原始记录的复验方）。"""
        from .policy import validate_policy

        compiled = validate_policy(package["policy_snapshot"])
        if compiled.policy_fingerprint != package["task"]["policy_fingerprint"]:
            raise ValueError("包内策略快照与声明指纹不符")
        ctx = self._ctx(compiled, package["task"]["purpose"])
        return export_records(records, compiled, package["task"]["purpose"], ctx)


def _sign_body(keys: LocalKeys, body: dict) -> dict:
    from .signing import sign

    sig = sign(keys.sign_private, body)
    sig["key_id"] = keys.key_id
    sig["public_key_spki_hex"] = keys.public_spki_hex()
    return sig


# ---- 复验 ------------------------------------------------------------------

def _digest_checks(package: dict) -> List[dict]:
    """输出与决策记录的整体摘要必须与清单一致（原始记录不随包分发）。"""
    m = package["manifest"]
    results = []
    for name, obj in (("output_digest", package.get("output")),
                      ("decisions_digest", package.get("decisions"))):
        actual = sha256_hex(canonical(obj))
        ok = actual == m.get(name)
        results.append({
            "check": name,
            "status": "passed" if ok else "failed",
            "expected": m.get(name),
            "actual": actual,
        })
    return results


def _decision_consistency(package: dict) -> dict:
    """输出与决策记录逐字段一致：present 叶子的输出摘要必须能重算且路径可达。"""
    errors = []
    outputs = package.get("output", [])
    decisions = package.get("decisions", [])
    present = 0
    for d in decisions:
        if d.get("structural"):
            continue
        if d.get("decision") in ("allow", "generalize"):
            present += 1
            value = _reach(outputs[d["record_index"]], d["path"])
            if value is _MISSING:
                errors.append(
                    f"{d['path']}: {d['decision']} 决策要求值出现在输出中，但未找到")
                continue
            digest = d.get("output_digest")
            if not digest:
                errors.append(f"{d['path']}: {d['decision']} 决策缺少 output_digest")
            elif sha256_hex(canonical(value)) != digest:
                errors.append(f"{d['path']}: 输出值与 output_digest 不一致")
        elif d.get("decision") == "deny":
            if d.get("action") == "generalize":
                continue  # 泛化失败导致的拒绝：值本就不应出现
            value = _reach(outputs[d["record_index"]], d["path"])
            if value is not _MISSING:
                errors.append(f"{d['path']}: 决策为 deny，但该路径在输出中仍有值")
    return {
        "check": "decision_output_consistency",
        "status": "passed" if not errors else "failed",
        "present_leaf_decisions": present,
        "total_decisions": len(decisions),
        "errors": errors,
    }


_MISSING = object()


def _reach(obj: Any, path: str) -> Any:
    """按实例路径（[k] 为具体下标）从输出中取值。"""
    from .paths import split_path

    try:
        tokens = split_path(path, allow_indices=True)
    except Exception:
        return _MISSING
    cur = obj
    for t in tokens:
        if t.startswith("[") and t.endswith("]") and t[1:-1].isdigit():
            i = int(t[1:-1])
            if not isinstance(cur, list) or i >= len(cur):
                return _MISSING
            cur = cur[i]
        else:
            from .paths import decode_segment

            k = decode_segment(t)
            if not isinstance(cur, dict) or k not in cur:
                return _MISSING
            cur = cur[k]
    return cur


def verify_package(
    package: Any,
    *,
    public_key=None,
    records: Optional[List[dict]] = None,
    keys: Optional[LocalKeys] = None,
) -> dict:
    """复验导出包。返回结构化报告，``overall_passed`` 为总判定。

    - public_key：验签公钥（缺省时使用包内嵌 SPKI，仅能防意外损坏，不能防伪造）
    - records + keys：提供时重放引擎做端到端复验
    """
    report: Dict[str, Any] = {"checks": [], "warnings": []}

    if not isinstance(package, dict):
        report["overall_passed"] = False
        report["checks"].append({"check": "shape", "status": "failed",
                                 "detail": "包不是 JSON 对象"})
        return report

    # 1. 结构
    need = {"task", "manifest", "policy_snapshot", "output", "decisions", "signature"}
    missing = need - set(package)
    report["checks"].append({
        "check": "required_fields",
        "status": "passed" if not missing else "failed",
        "missing": sorted(missing),
    })
    if missing:
        report["overall_passed"] = False
        return report

    # 2. 策略快照指纹
    from .policy import validate_policy, PolicyError

    try:
        compiled = validate_policy(package["policy_snapshot"])
        snap_ok = compiled.policy_fingerprint == package["task"]["policy_fingerprint"]
        report["checks"].append({
            "check": "policy_snapshot_fingerprint",
            "status": "passed" if snap_ok else "failed",
            "expected": package["task"]["policy_fingerprint"],
            "actual": compiled.policy_fingerprint,
        })
    except PolicyError as e:
        compiled = None
        report["checks"].append({"check": "policy_snapshot_fingerprint",
                                 "status": "failed", "detail": str(e)})

    # 3. 签名
    signed_part = {k: v for k, v in package.items() if k != "signature"}
    sig = dict(package["signature"])
    if public_key is None:
        try:
            from cryptography.hazmat.primitives.serialization import load_der_public_key

            public_key = load_der_public_key(bytes.fromhex(sig.get("public_key_spki_hex", "")))
            report["warnings"].append(
                "未提供验签公钥，使用包内嵌公钥仅能检测意外损坏，无法识别伪造包")
        except Exception:
            public_key = None
    if public_key is not None:
        embedded = sig.pop("key_id", None), sig.pop("public_key_spki_hex", None)
        ok, detail = sig_verify(public_key, signed_part, sig)
        sig["key_id"], sig["public_key_spki_hex"] = embedded
        report["checks"].append({"check": "ed25519_signature",
                                 "status": "passed" if ok else "failed", "detail": detail})
    else:
        report["checks"].append({"check": "ed25519_signature", "status": "failed",
                                 "detail": "无法加载验签公钥"})

    # 4. 摘要与决策一致性
    report["checks"].extend(_digest_checks(package))
    report["checks"].append(_decision_consistency(package))

    # 5. 原始记录摘要
    if records is not None:
        actual = sha256_hex(canonical(records))
        report["checks"].append({
            "check": "records_digest",
            "status": "passed" if actual == package["manifest"]["records_digest"] else "failed",
            "expected": package["manifest"]["records_digest"],
            "actual": actual,
        })

    # 6. 端到端重放
    if records is not None and keys is not None and compiled is not None:
        ctx = TransformContext(
            transform_secret=keys.transform_secret,
            policy_fingerprint=compiled.policy_fingerprint,
            purpose=package["task"]["purpose"],
        )
        re_out, re_dec = export_records(records, compiled,
                                        package["task"]["purpose"], ctx)
        out_ok = canonical(re_out) == canonical(package["output"])
        dec_ok = canonical(re_dec) == canonical(package["decisions"])
        report["checks"].append({"check": "replay_output", "status": "passed" if out_ok else "failed"})
        report["checks"].append({"check": "replay_decisions",
                                 "status": "passed" if dec_ok else "failed"})
    elif records is not None or keys is not None:
        report["warnings"].append("端到端重放需要同时提供 records 与 keys，已跳过")

    failed = [c for c in report["checks"] if c.get("status") == "failed"]
    report["overall_passed"] = not failed
    return report


def save_package(package: dict, path: Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(package, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)
    return path
