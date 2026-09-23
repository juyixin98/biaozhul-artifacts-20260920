"""编排层: 签名验证、事件去重、确定性重算、版本化与报告签名。"""
from __future__ import annotations

import hashlib

from cryptography.hazmat.primitives.serialization import (
    Encoding,
    PublicFormat,
)

from .crypto import (
    canonical_json,
    sign_payload,
    verify_payload,
)
from .engine import build_report, report_hash, validate_event


class ReplayService:
    def __init__(self, repo, trusted_signers: dict, server_private_key=None) -> None:
        self.repo = repo
        # hex 公钥(小写) -> Ed25519PublicKey
        self.trusted_signers = trusted_signers
        self.server_private_key = server_private_key

    # -------------------------------------------------- 验签

    def _verify_event(self, ev: dict) -> str | None:
        """返回 None 表示通过; 否则返回拒绝原因。"""
        try:
            signer_hex = str(ev["signer"]).strip().lower()
            signature = str(ev["signature"])
            pub = self.trusted_signers.get(signer_hex)
            if pub is None:
                return "UNTRUSTED_SIGNER"
            signed = {k: ev[k] for k in ("event_id", "ts", "seq", "action", "payload")}
            if not verify_payload(pub, signed, signature):
                return "BAD_SIGNATURE"
        except (KeyError, TypeError, ValueError):
            return "MALFORMED_ENVELOPE"
        return None

    # -------------------------------------------------- 写入 + 重算

    def ingest(self, events: list[dict]) -> dict:
        # 1) 批次内自身去重 (event_id 首次出现为准)
        seen: set[str] = set()
        deduped: list[dict] = []
        intra_dupes: list[str] = []
        for ev in events:
            eid = ev.get("event_id") if isinstance(ev, dict) else None
            if eid in seen:
                intra_dupes.append(eid)
                continue
            seen.add(eid)
            deduped.append(ev)

        # 2) 数据库已存在 -> duplicates (不再次验签)
        existing = self.repo.get_existing_ids([e.get("event_id") for e in deduped])
        rejected: list[dict] = []
        to_insert: list[dict] = []
        accepted_ids: list[str] = []
        for ev in deduped:
            eid = ev.get("event_id")
            if eid in existing:
                intra_dupes.append(eid)
                continue
            crypto_reason = self._verify_event(ev)
            if crypto_reason is not None:
                rejected.append({"event_id": eid, "reason": crypto_reason})
                continue
            biz_reason = validate_event(ev)
            if biz_reason is not None:
                rejected.append({"event_id": eid, "reason": biz_reason})
                continue
            to_insert.append(ev)
            accepted_ids.append(eid)

        self.repo.insert_events(to_insert)

        # 3) 始终对 *全部* 历史事件做一次确定性重算 (迟到事件在此修正历史)
        previous_version = self.repo.get_latest_version()
        prev_report = (
            self.repo.get_report(previous_version) if previous_version else None
        )
        prev_hash = prev_report["hash"] if prev_report else None
        prev_as_of = prev_report["body"]["as_of"] if prev_report else None

        all_events = self.repo.all_engine_events()
        if not all_events and previous_version is None:
            return {
                "accepted": len(accepted_ids),
                "duplicates": sorted(d for d in intra_dupes if d is not None),
                "rejected": rejected,
                "latest_version": None,
                "previous_version": None,
                "report_changed": False,
                "hash": None,
                "changed_reasons": ["NO_EVENTS"],
                "report": None,
            }

        body = build_report(all_events)
        new_hash = report_hash(body)

        changed_reasons: list[str] = []
        late = [
            e for e in to_insert if prev_as_of is not None and e["ts"] < prev_as_of
        ]
        if late:
            changed_reasons.append(
                "LATE_EVENT:" + ",".join(sorted(e["event_id"] for e in late))
            )

        report_changed = new_hash != prev_hash
        if report_changed:
            changed_reasons.append("OUTPUT_CHANGED")

        latest_version = previous_version
        if prev_hash is None or report_changed:
            if prev_hash is None:
                change_reason = "INITIAL"
            elif late:
                change_reason = "LATE_EVENT_RECOMPUTE"
            else:
                change_reason = "NEW_EVENTS"
            signature = sign_payload(self.server_private_key, body)
            latest_version = self.repo.save_report(body, signature, change_reason)
            changed_reasons.append(f"VERSION_CREATED:{latest_version}")

        return {
            "accepted": len(accepted_ids),
            "duplicates": sorted(d for d in intra_dupes if d is not None),
            "rejected": rejected,
            "latest_version": latest_version,
            "previous_version": previous_version,
            "report_changed": report_changed,
            "hash": new_hash,
            "changed_reasons": changed_reasons,
            "report": body,
        }

    # -------------------------------------------------- 读

    def replay_preview(self, as_of: int | None = None) -> dict:
        """对当前存储事件做只读回放 (不生成版本、不签名)。"""
        return build_report(self.repo.all_engine_events(), as_of=as_of)

    def verify_report(self, version: int) -> dict | None:
        rec = self.repo.get_report(version)
        if rec is None:
            return None
        body = rec["body"]
        body_for_hash = {k: v for k, v in body.items() if k != "hash"}
        recomputed = hashlib.sha256(canonical_json(body_for_hash)).hexdigest()
        pub = self.server_private_key.public_key()
        sig_ok = verify_payload(pub, body, rec["signature"])
        signer_hex = pub.public_bytes(Encoding.Raw, PublicFormat.Raw).hex()
        return {
            "report_signature_valid": sig_ok,
            "signer_hex": signer_hex,
            "hash_matches_body": recomputed == rec["hash"] == body["hash"],
        }
