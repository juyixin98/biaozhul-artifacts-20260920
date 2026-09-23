"""对账核心逻辑。

流程（每次 POST /reconcile 在一个事务内执行）：
1. 按各链当前高度推进确认：PENDING 且 head >= block + confirmations 的事件转为 CONFIRMED，
   并为其追加正式账条目（ledger_entries，按 event_id 唯一，天然幂等）；
2. 取全部 CONFIRMED 事件，按 message_id 分组配对：LOCK<->MINT、BURN<->RELEASE，
   同组内按 (block_number, log_index) 排序后顺序配对；
3. 产出异常（只报告、不修正链上状态）：
   - 多余铸造            -> DUPLICATE_MINT
   - 有铸造无任何有效锁定 -> MINT_WITHOUT_LOCK
   - 锁定超时未配对       -> UNMATCHED_LOCK（按对端链高度判定）
   - 销毁超时未配对       -> UNMATCHED_BURN
   - 有释放无任何有效销毁 -> RELEASE_WITHOUT_BURN
   - 资产级锁铸不守恒     -> CONSERVATION_MISMATCH
4. 写入 Snapshot（含各链高度、各资产守恒视图、异常数），快照永久保留。
"""
from __future__ import annotations

from collections import defaultdict
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import settings
from .models import (Anomaly, AnomalyType, ChainHead, EntryStatus, EventStatus,
                     EventType, LedgerEntry, Pair, RawEvent, Snapshot, utcnow)


# ---------------------------------------------------------------- 事件摄取

def ingest_events(db: Session, events: list[dict]) -> tuple[list[RawEvent], list[dict]]:
    """批量摄取，按 (chain, tx_hash, log_index) 去重；重复投递不重复计数。"""
    keys = {(e["chain"], e["tx_hash"], e["log_index"]) for e in events}
    existing = set()
    if keys:
        rows = db.execute(
            select(RawEvent.chain, RawEvent.tx_hash, RawEvent.log_index).where(
                RawEvent.chain.in_([k[0] for k in keys])
            )
        ).all()
        existing = {(c, t, l) for c, t, l in rows if (c, t, l) in keys}

    new_events: list[RawEvent] = []
    duplicates: list[dict] = []
    seen_in_batch: set[tuple] = set()
    for e in events:
        key = (e["chain"], e["tx_hash"], e["log_index"])
        if key in existing or key in seen_in_batch:
            duplicates.append(e)
            continue
        seen_in_batch.add(key)
        ev = RawEvent(
            chain=e["chain"],
            tx_hash=e["tx_hash"],
            log_index=e["log_index"],
            block_number=e["block_number"],
            event_type=EventType(e["event_type"]),
            message_id=e["message_id"],
            source_chain=e["source_chain"],
            origin_contract=e["origin_contract"],
            token_id=str(e["token_id"]),
            amount=e["amount"],
            sender=e.get("sender"),
            recipient=e.get("recipient"),
            dest_chain=e.get("dest_chain"),
            status=EventStatus.PENDING,
        )
        db.add(ev)
        new_events.append(ev)
    db.flush()
    return new_events, duplicates


# ---------------------------------------------------------------- 链高度 / 分叉

def set_chain_head(db: Session, chain: str, height: int, confirmations: int | None = None) -> ChainHead:
    head = db.get(ChainHead, chain)
    if head is None:
        head = ChainHead(chain=chain, height=height,
                         confirmations=confirmations if confirmations is not None else settings.default_confirmations)
        db.add(head)
    else:
        head.height = height
        if confirmations is not None:
            head.confirmations = confirmations
    db.flush()
    return head


def get_heads(db: Session) -> dict[str, ChainHead]:
    return {h.chain: h for h in db.execute(select(ChainHead)).scalars()}


def apply_reorg(db: Session, chain: str, from_height: int) -> tuple[int, int, int]:
    """分叉撤销：from_height（含）起的事件标记 REVERTED，其账目标记 REVERTED（不删除）。"""
    events = db.execute(
        select(RawEvent).where(
            RawEvent.chain == chain,
            RawEvent.block_number >= from_height,
            RawEvent.status != EventStatus.REVERTED,
        )
    ).scalars().all()

    entry_count = 0
    for ev in events:
        ev.status = EventStatus.REVERTED
        entry = db.execute(
            select(LedgerEntry).where(LedgerEntry.event_id == ev.id)
        ).scalar_one_or_none()
        if entry is not None and entry.status == EntryStatus.ACTIVE:
            entry.status = EntryStatus.REVERTED
            entry_count += 1

    head = db.get(ChainHead, chain)
    new_head = head.height if head else 0
    if head and head.height >= from_height:
        head.height = from_height - 1
        new_head = head.height
    db.flush()
    return len(events), entry_count, new_head


# ---------------------------------------------------------------- 对账

def _evidence(ev: RawEvent) -> dict:
    return {
        "event_id": ev.id,
        "chain": ev.chain,
        "tx_hash": ev.tx_hash,
        "log_index": ev.log_index,
        "block_number": ev.block_number,
        "event_type": ev.event_type.value,
    }


def _confirm_due_events(db: Session, snapshot: Snapshot, heads: dict[str, ChainHead]) -> int:
    """把达到确认高度的 PENDING 事件转为 CONFIRMED 并写入正式账。"""
    pending = db.execute(
        select(RawEvent).where(RawEvent.status == EventStatus.PENDING)
    ).scalars().all()
    count = 0
    for ev in pending:
        head = heads.get(ev.chain)
        if head is None or head.height < ev.block_number + head.confirmations:
            continue
        ev.status = EventStatus.CONFIRMED
        ev.confirmed_at = utcnow()
        db.add(LedgerEntry(
            event_id=ev.id,
            snapshot_id=snapshot.id,
            chain=ev.chain,
            entry_type=ev.event_type,
            source_chain=ev.source_chain,
            origin_contract=ev.origin_contract,
            token_id=ev.token_id,
            amount=ev.amount,
            message_id=ev.message_id,
            status=EntryStatus.ACTIVE,
        ))
        count += 1
    db.flush()
    return count


def run_reconcile(db: Session, pairing_timeout_blocks: int | None = None) -> tuple[Snapshot, dict]:
    timeout = pairing_timeout_blocks if pairing_timeout_blocks is not None else settings.pairing_timeout_blocks
    heads = get_heads(db)

    snapshot = Snapshot(chain_heads={c: h.height for c, h in heads.items()})
    db.add(snapshot)
    db.flush()  # 取得 snapshot.id

    newly_confirmed = _confirm_due_events(db, snapshot, heads)

    confirmed = db.execute(
        select(RawEvent).where(RawEvent.status == EventStatus.CONFIRMED)
    ).scalars().all()
    # 是否存在尚未确认/未作废的锁定或销毁（用于区分“乱序未到”与“根本不存在”）
    pending_types: dict[str, set[EventType]] = defaultdict(set)
    for ev in db.execute(select(RawEvent).where(RawEvent.status == EventStatus.PENDING)).scalars().all():
        pending_types[ev.message_id].add(ev.event_type)

    groups: dict[str, list[RawEvent]] = defaultdict(list)
    for ev in confirmed:
        groups[ev.message_id].append(ev)

    anomalies: list[Anomaly] = []
    pairs_formed = 0

    def add_anomaly(atype: AnomalyType, evs: list[RawEvent], detail: str, message_id: str | None):
        first = evs[0]
        anomalies.append(Anomaly(
            snapshot_id=snapshot.id,
            anomaly_type=atype,
            message_id=message_id,
            source_chain=first.source_chain,
            origin_contract=first.origin_contract,
            token_id=first.token_id,
            detail=detail,
            evidence=[_evidence(e) for e in evs],
        ))

    for message_id, evs in groups.items():
        locks = sorted((e for e in evs if e.event_type == EventType.LOCK), key=lambda e: (e.block_number, e.log_index))
        mints = sorted((e for e in evs if e.event_type == EventType.MINT), key=lambda e: (e.block_number, e.log_index))
        burns = sorted((e for e in evs if e.event_type == EventType.BURN), key=lambda e: (e.block_number, e.log_index))
        releases = sorted((e for e in evs if e.event_type == EventType.RELEASE), key=lambda e: (e.block_number, e.log_index))

        # 顺序配对
        for lock, mint in zip(locks, mints):
            db.add(Pair(snapshot_id=snapshot.id, message_id=message_id,
                        source_chain=lock.source_chain, origin_contract=lock.origin_contract,
                        token_id=lock.token_id, lock_event_id=lock.id, mint_event_id=mint.id,
                        pair_kind="LOCK_MINTED"))
            pairs_formed += 1
        for burn, release in zip(burns, releases):
            db.add(Pair(snapshot_id=snapshot.id, message_id=message_id,
                        source_chain=burn.source_chain, origin_contract=burn.origin_contract,
                        token_id=burn.token_id, burn_event_id=burn.id, release_event_id=release.id,
                        pair_kind="BURN_RELEASED"))
            pairs_formed += 1

        extra_mints = mints[len(locks):]
        extra_locks = locks[len(mints):]
        extra_burns = burns[len(releases):]
        extra_releases = releases[len(burns):]

        if extra_mints:
            if locks:
                add_anomaly(AnomalyType.DUPLICATE_MINT, locks[:1] + mints,
                            f"关联消息 {message_id} 出现 {len(mints)} 次铸造，仅 {len(locks)} 次锁定", message_id)
            elif EventType.LOCK not in pending_types[message_id]:
                for mint in extra_mints:
                    add_anomaly(AnomalyType.MINT_WITHOUT_LOCK, [mint],
                                f"铸造 {mint.tx_hash} 无任何有效锁定事件与之配对", message_id)

        for lock in extra_locks:
            # 用锁定声明的对端链高度判定超时；未声明则跳过超时判定
            counterparty = heads.get(lock.dest_chain) if lock.dest_chain else None
            if counterparty and counterparty.height >= lock.block_number + timeout:
                add_anomaly(AnomalyType.UNMATCHED_LOCK, [lock],
                            f"锁定于 {lock.chain}#{lock.block_number}，对端链 {lock.dest_chain} "
                            f"高度 {counterparty.height} 已超 {timeout} 块仍未见铸造", message_id)

        for burn in extra_burns:
            counterparty = heads.get(burn.dest_chain) if burn.dest_chain else heads.get(burn.source_chain)
            if counterparty and counterparty.height >= burn.block_number + timeout:
                add_anomaly(AnomalyType.UNMATCHED_BURN, [burn],
                            f"销毁于 {burn.chain}#{burn.block_number}，源链高度 "
                            f"{counterparty.height} 已超 {timeout} 块仍未见释放", message_id)

        if extra_releases and EventType.BURN not in pending_types[message_id]:
            for release in extra_releases:
                add_anomaly(AnomalyType.RELEASE_WITHOUT_BURN, [release],
                            f"释放 {release.tx_hash} 无任何有效销毁事件与之配对", message_id)

    # 资产级守恒视图：net_locked == net_minted 才守恒
    per_asset: dict[tuple, dict] = defaultdict(lambda: {"locked": Decimal(0), "released": Decimal(0),
                                                        "minted": Decimal(0), "burned": Decimal(0),
                                                        "events": []})
    for ev in confirmed:
        key = (ev.source_chain, ev.origin_contract, ev.token_id)
        slot = per_asset[key]
        if ev.event_type == EventType.LOCK:
            slot["locked"] += ev.amount
        elif ev.event_type == EventType.RELEASE:
            slot["released"] += ev.amount
        elif ev.event_type == EventType.MINT:
            slot["minted"] += ev.amount
        elif ev.event_type == EventType.BURN:
            slot["burned"] += ev.amount
        slot["events"].append(ev)

    totals = []
    for (source_chain, contract, token_id), slot in sorted(per_asset.items()):
        net_locked = slot["locked"] - slot["released"]
        net_minted = slot["minted"] - slot["burned"]
        delta = net_locked - net_minted
        totals.append({
            "source_chain": source_chain,
            "origin_contract": contract,
            "token_id": token_id,
            "locked": int(slot["locked"]), "released": int(slot["released"]),
            "minted": int(slot["minted"]), "burned": int(slot["burned"]),
            "net_locked": int(net_locked), "net_minted": int(net_minted),
            "delta": int(delta),
        })
        if delta != 0:
            add_anomaly(AnomalyType.CONSERVATION_MISMATCH, slot["events"],
                        f"资产 {source_chain}:{contract}:{token_id} 净锁定 {net_locked} "
                        f"与净铸造 {net_minted} 差 {delta}", None)

    snapshot.totals = totals
    snapshot.anomaly_count = len(anomalies)
    for a in anomalies:
        db.add(a)
    db.flush()

    return snapshot, {
        "snapshot_id": snapshot.id,
        "newly_confirmed": newly_confirmed,
        "pairs_formed": pairs_formed,
        "anomaly_count": len(anomalies),
        "totals": totals,
    }
