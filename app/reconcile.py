"""核心对账器：确认度判定、跨链配对、守恒校验、证据与快照。

设计原则：
- 只读取已入库的链上事件与块头，离线计算；**绝不写链、不自动修正**。
- 每次运行全量重算当前状态，结果写入 pairings/findings，并落盘快照+证据。
- 所有金额用 Python int 精确汇总；快照用规范 JSON + SHA-256 哈希链。
"""
from __future__ import annotations

import json
import os
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import chains as chains_svc
from .config import get_settings
from .crypto import canonical_json, sha256_hex
from .models import Chain, ChainEvent, Finding, Pairing, RawEvent, Reconciliation

ORIGIN_SIDE_EVENTS = {"LOCK", "RELEASE"}      # 原始资产侧动作，必须发生在 source_chain
MAPPED_SIDE_EVENTS = {"MINT", "BURN"}         # 映射资产侧动作，必须发生在非 source_chain


def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def _event_ref(e: ChainEvent) -> dict:
    return {
        "event_key": e.event_key,
        "chain_id": e.chain_id,
        "event_type": e.event_type,
        "tx_hash": e.tx_hash,
        "log_index": e.log_index,
        "block_height": e.block_height,
        "block_hash": e.block_hash,
        "source_chain": e.source_chain,
        "original_contract": e.original_contract,
        "token_id": e.token_id,
        "asset_uid": e.asset_uid,
        "message_id": e.message_id,
        "source_nonce": e.source_nonce,
        "account": e.account,
        "amount": str(e.amount),
        "status": e.status,
        "first_seen_at": _iso(e.first_seen_at),
    }


def _refresh_confirmation(
    db: Session, chain: Chain, canonical: dict, tip_height: int | None
) -> dict[str, ChainEvent]:
    """按规范链与确认深度刷新该链全部事件状态，返回 {event_key: event}。"""
    events = db.scalars(select(ChainEvent).where(ChainEvent.chain_id == chain.chain_id)).all()
    out: dict[str, ChainEvent] = {}
    for e in events:
        e.prev_status = e.status
        block = canonical.get(e.block_hash)
        if block is not None:
            assert tip_height is not None
            confirmed = tip_height - block.block_height >= chain.confirmation_depth
            e.canonical_block_hash = block.block_hash
            e.status = "CONFIRMED" if confirmed else "PENDING"
        else:
            e.canonical_block_hash = None
            # 块头已登记但不在规范链 => 分叉；块头未登记 => 暂不可判定
            from .models import Block as _Block
            header = db.scalar(
                select(_Block).where(
                    _Block.chain_id == chain.chain_id, _Block.block_hash == e.block_hash
                )
            )
            e.status = "REORGED" if header is not None else "PENDING"
        out[e.event_key] = e
    return out


def run_reconciliation(db: Session, as_of: datetime | None = None) -> dict:
    settings = get_settings()
    snapshot_dir = Path(settings.snapshot_dir)
    evidence_dir = Path(settings.evidence_dir)
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    evidence_dir.mkdir(parents=True, exist_ok=True)

    started = datetime.now(timezone.utc)
    as_of = (as_of or started).astimezone(timezone.utc)
    run_id = f"run-{started.strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:12]}"
    run_evidence_dir = evidence_dir / run_id
    run_evidence_dir.mkdir(parents=True, exist_ok=True)

    prev = db.scalars(
        select(Reconciliation).order_by(Reconciliation.id.desc()).limit(1)
    ).first()
    prev_hash = prev.snapshot_hash if prev else ""

    recon = Reconciliation(
        run_id=run_id,
        started_at=started,
        as_of=as_of,
        prev_snapshot_hash=prev_hash,
    )
    db.add(recon)
    db.flush()

    all_events: dict[str, ChainEvent] = {}
    chain_views: list[dict] = []
    chains = db.scalars(select(Chain).order_by(Chain.chain_id)).all()

    for chain in chains:
        canonical, tip_height = chains_svc.canonical_block_map(db, chain.chain_id)
        events_map = _refresh_confirmation(db, chain, canonical, tip_height)
        all_events.update(events_map)

        # 分叉撤销：曾经计入正式账，现被甩出规范链
        for e in events_map.values():
            if e.prev_status == "CONFIRMED" and e.status == "REORGED":
                _add_finding(
                    db, recon, run_evidence_dir,
                    code="REORGED_EVENT", severity="high",
                    message_id=e.message_id, asset_uid=e.asset_uid,
                    detail=(
                        f"{e.event_type} event on chain {e.chain_id} was CONFIRMED but its "
                        f"block {e.block_hash}@{e.block_height} is no longer canonical"
                    ),
                    evidence={
                        "rule": "event reverted by chain reorganization",
                        "chain_id": chain.chain_id,
                        "current_tip_hash": chain.current_tip_hash,
                        "tip_height": tip_height,
                        "confirmation_depth": chain.confirmation_depth,
                        "event": _event_ref(e),
                        "prev_status": "CONFIRMED",
                    },
                )

        counts = defaultdict(int)
        for e in events_map.values():
            counts[e.status] += 1
        chain_views.append({
            "chain_id": chain.chain_id,
            "current_tip_hash": chain.current_tip_hash,
            "tip_height": tip_height,
            "confirmation_depth": chain.confirmation_depth,
            "message_timeout_seconds": chain.message_timeout_seconds,
            "canonical_chain_height_span": (
                [min(b.block_height for b in canonical.values()),
                 max(b.block_height for b in canonical.values())]
                if canonical else None
            ),
            "status_counts": dict(counts),
        })

    confirmed = [e for e in all_events.values() if e.status == "CONFIRMED"]

    # ---- 分组：(message_id, asset_uid) 与 message_id 维度 ----------------
    groups: dict[tuple[str, str], dict[str, list[ChainEvent]]] = defaultdict(
        lambda: defaultdict(list)
    )
    observed_groups: dict[tuple[str, str], dict[str, list[ChainEvent]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for e in all_events.values():
        if e.status != "REORGED":
            observed_groups[(e.message_id, e.asset_uid)][e.event_type].append(e)
    for e in confirmed:
        groups[(e.message_id, e.asset_uid)][e.event_type].append(e)

    pairings_out: list[dict] = []

    def _persist_pairing(p: dict) -> None:
        db.add(Pairing(
            reconciliation_id=recon.id,
            message_id=p["message_id"], asset_uid=p["asset_uid"],
            direction=p["direction"], state=p["state"],
            lock_event_key=p.get("lock_event_key"), mint_event_key=p.get("mint_event_key"),
            burn_event_key=p.get("burn_event_key"), release_event_key=p.get("release_event_key"),
            lock_amount=p.get("lock_amount"), mint_amount=p.get("mint_amount"),
            burn_amount=p.get("burn_amount"), release_amount=p.get("release_amount"),
        ))
        pairings_out.append(p)

    def _first(group: dict[str, list[ChainEvent]], t: str) -> ChainEvent | None:
        lst = group.get(t) or []
        return sorted(lst, key=lambda x: (x.block_height, x.log_index))[0] if lst else None

    # ---- 逐消息配对 -------------------------------------------------------
    for (mid, uid), group in sorted(groups.items()):
        observed = observed_groups[(mid, uid)]
        locks = sorted(group.get("LOCK", []), key=lambda x: (x.block_height, x.log_index))
        mints = sorted(group.get("MINT", []), key=lambda x: (x.block_height, x.log_index))
        burns = sorted(group.get("BURN", []), key=lambda x: (x.block_height, x.log_index))
        releases = sorted(group.get("RELEASE", []), key=lambda x: (x.block_height, x.log_index))

        if locks or mints:
            lock = locks[0] if locks else None
            mint = mints[0] if mints else None

            # 动作必须发生在正确的一侧链
            for ev in (lock, mint):
                if ev is not None:
                    _check_event_chain_side(db, recon, run_evidence_dir, ev)

            # 同消息多锁（协议异常）
            for extra in locks[1:]:
                _add_finding(
                    db, recon, run_evidence_dir,
                    code="MULTIPLE_LOCK_SAME_MESSAGE", severity="high",
                    message_id=mid, asset_uid=uid,
                    detail=f"multiple confirmed LOCK events share message {mid[:16]}…",
                    evidence={"events": [_event_ref(x) for x in locks]},
                )

            pair = {
                "message_id": mid, "asset_uid": uid, "direction": "LOCK_MINT",
                "state": "HALF_OPEN",
                "lock_event_key": lock.event_key if lock else None,
                "mint_event_key": mint.event_key if mint else None,
                "lock_amount": lock.amount if lock else None,
                "mint_amount": mint.amount if mint else None,
            }

            if lock and mint:
                pair["state"] = "PAIRED"
                if lock.amount != mint.amount:
                    _add_finding(
                        db, recon, run_evidence_dir,
                        code="AMOUNT_MISMATCH", severity="high",
                        message_id=mid, asset_uid=uid,
                        detail=(
                            f"LOCK amount {lock.amount} != MINT amount {mint.amount} "
                            f"for message {mid[:16]}…"
                        ),
                        evidence={"lock": _event_ref(lock), "mint": _event_ref(mint),
                                  "lock_amount": str(lock.amount),
                                  "mint_amount": str(mint.amount),
                                  "difference": str(abs(lock.amount - mint.amount))},
                    )
            elif lock and not mint:
                if observed.get("MINT"):
                    pair["state"] = "HALF_OPEN"  # 对侧已见但未确认（乱序）
                elif _timed_out(db, lock, as_of):
                    pair["state"] = "TIMEOUT"
                    _add_finding(
                        db, recon, run_evidence_dir,
                        code="UNPAIRED_LOCK_TIMEOUT", severity="high",
                        message_id=mid, asset_uid=uid,
                        detail=(
                            f"confirmed LOCK has no MINT after timeout "
                            f"(message {mid[:16]}…)"
                        ),
                        evidence={
                            "rule": "unpaired lock past message timeout",
                            "as_of": _iso(as_of),
                            "timeout_seconds": _chain_timeout(db, lock.chain_id),
                            "lock_block_time": _block_time_iso(db, lock),
                            "lock": _event_ref(lock),
                        },
                    )
            elif mint and not lock:
                if observed.get("LOCK"):
                    pair["state"] = "HALF_OPEN"  # LOCK 已见但未确认（乱序）
                else:
                    pair["state"] = "MINT_WITHOUT_LOCK"
                    _add_finding(
                        db, recon, run_evidence_dir,
                        code="MINT_WITHOUT_LOCK", severity="high",
                        message_id=mid, asset_uid=uid,
                        detail=(
                            f"confirmed MINT on {mint.chain_id} has no LOCK event on "
                            f"source chain {mint.source_chain}"
                        ),
                        evidence={
                            "rule": "mint must be backed by a lock with same message_id/asset",
                            "as_of": _iso(as_of),
                            "mint": _event_ref(mint),
                            "searched": "all observed LOCK events for message_id+asset_uid",
                        },
                    )

            # 重复铸造：同一 LOCK 消息出现多个已确认 MINT
            for extra in mints[1:]:
                _add_finding(
                    db, recon, run_evidence_dir,
                    code="DUPLICATE_MINT", severity="high",
                    message_id=mid, asset_uid=uid,
                    detail=(
                        f"{len(mints)} confirmed MINT events for one LOCK message "
                        f"{mid[:16]}…"
                    ),
                    evidence={
                        "rule": "one lock message may fund at most one mint",
                        "lock": _event_ref(lock) if lock else None,
                        "first_mint": _event_ref(mint) if mint else None,
                        "duplicate_mints": [_event_ref(x) for x in mints[1:]],
                        "all_mints": [_event_ref(x) for x in mints],
                    },
                )
            _persist_pairing(pair)

        if burns or releases:
            burn = burns[0] if burns else None
            release = releases[0] if releases else None
            for ev in (burn, release):
                if ev is not None:
                    _check_event_chain_side(db, recon, run_evidence_dir, ev)

            for extra in burns[1:]:
                _add_finding(
                    db, recon, run_evidence_dir,
                    code="DUPLICATE_BURN", severity="high",
                    message_id=mid, asset_uid=uid,
                    detail=f"multiple confirmed BURN events share message {mid[:16]}…",
                    evidence={"events": [_event_ref(x) for x in burns]},
                )

            pair = {
                "message_id": mid, "asset_uid": uid, "direction": "BURN_RELEASE",
                "state": "HALF_OPEN",
                "burn_event_key": burn.event_key if burn else None,
                "release_event_key": release.event_key if release else None,
                "burn_amount": burn.amount if burn else None,
                "release_amount": release.amount if release else None,
            }
            if burn and release:
                pair["state"] = "PAIRED"
                if burn.amount != release.amount:
                    _add_finding(
                        db, recon, run_evidence_dir,
                        code="AMOUNT_MISMATCH", severity="high",
                        message_id=mid, asset_uid=uid,
                        detail=f"BURN {burn.amount} != RELEASE {release.amount}",
                        evidence={"burn": _event_ref(burn), "release": _event_ref(release),
                                  "burn_amount": str(burn.amount),
                                  "release_amount": str(release.amount),
                                  "difference": str(abs(burn.amount - release.amount))},
                    )
            elif burn and not release:
                if observed.get("RELEASE"):
                    pair["state"] = "HALF_OPEN"
                elif _timed_out(db, burn, as_of):
                    pair["state"] = "TIMEOUT"
                    _add_finding(
                        db, recon, run_evidence_dir,
                        code="UNPAIRED_BURN_TIMEOUT", severity="high",
                        message_id=mid, asset_uid=uid,
                        detail="confirmed BURN has no RELEASE after timeout",
                        evidence={"as_of": _iso(as_of),
                                  "timeout_seconds": _chain_timeout(db, burn.chain_id),
                                  "burn_block_time": _block_time_iso(db, burn),
                                  "burn": _event_ref(burn)},
                    )
            elif release and not burn:
                if observed.get("BURN"):
                    pair["state"] = "HALF_OPEN"
                else:
                    pair["state"] = "RELEASE_WITHOUT_BURN"
                    _add_finding(
                        db, recon, run_evidence_dir,
                        code="RELEASE_WITHOUT_BURN", severity="high",
                        message_id=mid, asset_uid=uid,
                        detail="confirmed RELEASE has no BURN event",
                        evidence={"as_of": _iso(as_of), "release": _event_ref(release)},
                    )
            _persist_pairing(pair)

    # ---- 同一 message_id 跨资产（消息被复用于不同 UID）--------------------
    # 在全部已观察（非分叉）事件上检测：哪怕其中一支尚未确认，消息复用本身已可见。
    msg_assets_observed: dict[str, set[str]] = defaultdict(set)
    for e in all_events.values():
        if e.status != "REORGED":
            msg_assets_observed[e.message_id].add(e.asset_uid)
    for mid, uids in sorted(msg_assets_observed.items()):
        if len(uids) > 1:
            evs = [e for e in all_events.values()
                   if e.message_id == mid and e.status != "REORGED"]
            n_confirmed = sum(1 for e in evs if e.status == "CONFIRMED")
            # 两支都确认 => high；尚有未确认 => medium（待观察）
            severity = "high" if n_confirmed == len(evs) else "medium"
            _add_finding(
                db, recon, run_evidence_dir,
                code="MESSAGE_ASSET_MISMATCH", severity=severity,
                message_id=mid, asset_uid=None,
                detail=(f"message {mid[:16]}… used across {len(uids)} distinct assets "
                        f"({n_confirmed}/{len(evs)} events confirmed)"),
                evidence={"asset_uids": sorted(uids),
                          "events": [_event_ref(e) for e in evs]},
            )

    # ---- 资产级守恒（独立于配对的总额校验）-------------------------------
    conservation: dict[str, dict] = {}
    by_asset: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for e in confirmed:
        by_asset[e.asset_uid][e.event_type] += e.amount
    for uid, tallies in sorted(by_asset.items()):
        lock_mint = tallies["LOCK"] - tallies["MINT"]
        burn_release = tallies["BURN"] - tallies["RELEASE"]
        conservation[uid] = {
            "lock_total": str(tallies["LOCK"]),
            "mint_total": str(tallies["MINT"]),
            "burn_total": str(tallies["BURN"]),
            "release_total": str(tallies["RELEASE"]),
            "lock_minus_mint": str(lock_mint),
            "burn_minus_release": str(burn_release),
        }
        if lock_mint != 0 or burn_release != 0:
            # 在途（对侧事件尚未确认或尚未到达且未超时）的配对会造成总额暂时不对等：
            # 这类「时差性失衡」已由 HALF_OPEN 配对体现，不再重复报 CONSERVATION_IMBALANCE。
            # 只有不存在任何在途配对时仍失衡，才是真正的守恒违例。
            in_flight = any(
                p["asset_uid"] == uid and p["state"] == "HALF_OPEN"
                for p in pairings_out
            )
            if not in_flight:
                related = [_event_ref(e) for e in confirmed if e.asset_uid == uid]
                _add_finding(
                    db, recon, run_evidence_dir,
                    code="CONSERVATION_IMBALANCE", severity="high",
                    message_id=None, asset_uid=uid,
                    detail=(
                        f"asset conservation imbalance: LOCK-MINT={lock_mint}, "
                        f"BURN-RELEASE={burn_release}"
                    ),
                    evidence={"totals": conservation[uid], "confirmed_events": related},
                )

    # ---- 投递统计 ---------------------------------------------------------
    delivery = {
        "raw_events": db.scalar(select(RawEvent).limit(1)) is not None,
        "total_deliveries": 0,
        "distinct_events": 0,
        "payload_conflicts": 0,
    }
    raws = db.scalars(select(RawEvent)).all()
    delivery["distinct_events"] = len(raws)
    delivery["total_deliveries"] = sum(r.delivery_count for r in raws)
    delivery["payload_conflicts"] = sum(1 for r in raws if r.payload_conflict)

    findings = db.scalars(
        select(Finding).where(Finding.reconciliation_id == recon.id)
    ).all()

    finished = datetime.now(timezone.utc)
    snapshot_body = {
        "schema": "recon-snapshot/v1",
        "run_id": run_id,
        "started_at": _iso(started),
        "finished_at": _iso(finished),
        "as_of": _iso(as_of),
        "prev_snapshot_hash": prev_hash,
        "chains": chain_views,
        "events": [_event_ref(e) for e in sorted(all_events.values(), key=lambda x: x.id)],
        "pairings": pairings_out,
        "conservation": conservation,
        "delivery": delivery,
        "findings": [
            {
                "code": f.code,
                "severity": f.severity,
                "message_id": f.message_id,
                "asset_uid": f.asset_uid,
                "detail": f.detail,
                "evidence_path": f.evidence_path,
            }
            for f in findings
        ],
    }
    snap_hash = sha256_hex(canonical_json(snapshot_body))
    snapshot_body["snapshot_hash"] = snap_hash

    snap_path = snapshot_dir / f"{run_id}.json"
    with open(snap_path, "w", encoding="utf-8") as fh:
        json.dump(snapshot_body, fh, ensure_ascii=False, indent=2, sort_keys=True)
        fh.write("\n")

    recon.finished_at = finished
    recon.snapshot_hash = snap_hash
    recon.snapshot_path = str(snap_path)
    recon.summary_json = canonical_json({
        "run_id": run_id,
        "snapshot_hash": snap_hash,
        "prev_snapshot_hash": prev_hash,
        "finding_count": len(findings),
        "findings_by_code": {
            code: sum(1 for f in findings if f.code == code)
            for code in sorted({f.code for f in findings})
        },
        "events_confirmed": sum(1 for e in all_events.values() if e.status == "CONFIRMED"),
        "events_pending": sum(1 for e in all_events.values() if e.status == "PENDING"),
        "events_reorged": sum(1 for e in all_events.values() if e.status == "REORGED"),
        "pairs_paired": sum(1 for p in pairings_out if p["state"] == "PAIRED"),
        "pairs_half_open": sum(1 for p in pairings_out if p["state"] == "HALF_OPEN"),
        "pairs_timeout": sum(1 for p in pairings_out if p["state"] == "TIMEOUT"),
    }).decode("utf-8")
    db.flush()

    return {
        "run_id": run_id,
        "snapshot_hash": snap_hash,
        "prev_snapshot_hash": prev_hash,
        "snapshot_path": str(snap_path),
        "as_of": _iso(as_of),
        "finding_count": len(findings),
        "findings": [
            {
                "code": f.code, "severity": f.severity, "message_id": f.message_id,
                "asset_uid": f.asset_uid, "detail": f.detail,
                "evidence_path": f.evidence_path,
            }
            for f in findings
        ],
    }


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------

def _add_finding(
    db: Session, recon: Reconciliation, run_evidence_dir: Path,
    *, code: str, severity: str, message_id: str | None, asset_uid: str | None,
    detail: str, evidence: dict,
) -> Finding:
    evidence_doc = {
        "schema": "evidence/v1",
        "code": code,
        "severity": severity,
        "run_id": recon.run_id,
        "message_id": message_id,
        "asset_uid": asset_uid,
        "detail": detail,
        "generated_at": _iso(datetime.now(timezone.utc)),
        "evidence": evidence,
    }
    n = len(list(run_evidence_dir.glob(f"{code}-*.json"))) + 1
    path = run_evidence_dir / f"{code}-{n:03d}.json"
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(evidence_doc, fh, ensure_ascii=False, indent=2, sort_keys=True)
        fh.write("\n")
    finding = Finding(
        reconciliation_id=recon.id,
        code=code, severity=severity,
        message_id=message_id, asset_uid=asset_uid,
        detail=detail, evidence_path=str(path),
    )
    db.add(finding)
    db.flush()
    return finding


def _check_event_chain_side(
    db: Session, recon: Reconciliation, run_evidence_dir: Path, e: ChainEvent
) -> None:
    """LOCK/RELEASE 必须在 source_chain；MINT/BURN 必须在其它链。"""
    if e.event_type in ORIGIN_SIDE_EVENTS and e.chain_id != e.source_chain:
        _add_finding(
            db, recon, run_evidence_dir,
            code="EVENT_WRONG_CHAIN_SIDE", severity="high",
            message_id=e.message_id, asset_uid=e.asset_uid,
            detail=(
                f"{e.event_type} observed on {e.chain_id} but asset's source chain is "
                f"{e.source_chain}"
            ),
            evidence={"event": _event_ref(e)},
        )
    if e.event_type in MAPPED_SIDE_EVENTS and e.chain_id == e.source_chain:
        _add_finding(
            db, recon, run_evidence_dir,
            code="EVENT_WRONG_CHAIN_SIDE", severity="high",
            message_id=e.message_id, asset_uid=e.asset_uid,
            detail=f"{e.event_type} observed on the asset's own source chain {e.chain_id}",
            evidence={"event": _event_ref(e)},
        )


def _block_time_iso(db: Session, e: ChainEvent) -> str | None:
    from .models import Block
    block = db.scalar(
        select(Block).where(Block.chain_id == e.chain_id, Block.block_hash == e.block_hash)
    )
    return _iso(block.block_time) if block else None


def _chain_timeout(db: Session, chain_id: str) -> int:
    chain = db.get(Chain, chain_id)
    return chain.message_timeout_seconds if chain else 86400


def _timed_out(db: Session, e: ChainEvent, as_of: datetime) -> bool:
    from .models import Block
    block = db.scalar(
        select(Block).where(Block.chain_id == e.chain_id, Block.block_hash == e.block_hash)
    )
    reference = block.block_time if block else e.first_seen_at
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=timezone.utc)
    timeout = _chain_timeout(db, e.chain_id)
    return (as_of - reference.astimezone(timezone.utc)).total_seconds() >= timeout
