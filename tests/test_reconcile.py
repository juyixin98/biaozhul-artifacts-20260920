"""规定场景：乱序、超时未配对、分叉撤销、两链相同地址，以及守恒违例。"""
from __future__ import annotations

import json
from datetime import timedelta

from app.models import ChainEvent, Pairing, Reconciliation
from tests.factories import (
    BASE_TIME,
    block_hash,
    evidence_by_code,
    finding_codes,
    fork_hash,
    ingest,
    make_event,
    push_chain,
    reconcile,
    setup_chain,
)
from app import chains as chains_svc
from app.ingest import IngestError, ingest_event
from app.identity import asset_uid
import pytest


CONTRACT = "0xDEADBEEF"


def _happy_pair_setup(db, feeders, *, nonce="msg-1", mint_block=2, chain_b_tip=4):
    """两条链确认深度2；LOCK@A:1，MINT@B:mint_block；tip 均为 4。"""
    setup_chain(db, "chainA", feeders["chainA"])
    setup_chain(db, "chainB", feeders["chainB"])
    push_chain(db, "chainA", range(0, 5))
    push_chain(db, "chainB", range(0, 5), tip_height=chain_b_tip)
    lock = make_event(feeders["chainA"], chain_id="chainA", event_type="LOCK",
                      nonce=nonce, source_chain="chainA", contract=CONTRACT,
                      amount=100, block_height=1)
    mint = make_event(feeders["chainB"], chain_id="chainB", event_type="MINT",
                      nonce=nonce, source_chain="chainA", contract=CONTRACT,
                      amount=100, block_height=mint_block)
    return lock, mint


def test_out_of_order_pairs(db, feeders):
    """乱序：MINT 先到、LOCK 后到；中间快照显示 half-open，最终正确配对且守恒。"""
    lock, mint = _happy_pair_setup(db, feeders, mint_block=2)

    # MINT 已确认但 LOCK 尚未到达 => 无锁定铸造（中间结论，快照保留）
    ingest(db, mint)
    r1 = reconcile(db)
    assert "MINT_WITHOUT_LOCK" in finding_codes(r1)
    p1 = db.query(Pairing).filter_by(reconciliation_id=_rid(db, r1)).one()
    assert p1.state == "MINT_WITHOUT_LOCK"

    # 之后 LOCK 才到（乱序投递）=> 重新对账后配对成功，无 finding
    ingest(db, lock)
    r2 = reconcile(db)
    assert r2["findings"] == [], r2["findings"]
    p2 = db.query(Pairing).filter_by(reconciliation_id=_rid(db, r2)).one()
    assert p2.state == "PAIRED"
    assert p2.lock_amount == 100 and p2.mint_amount == 100

    # 守恒为零
    snap = json.load(open(r2["snapshot_path"]))
    uid = asset_uid("chainA", CONTRACT, "")
    c = snap["conservation"][uid]
    assert c["lock_minus_mint"] == "0" and c["burn_minus_release"] == "0"


def test_out_of_order_unconfirmed_lock_is_half_open_not_violation(db, feeders):
    """LOCK 已投递但所在块未达确认深度：算 PENDING，记 half-open，不误报违例。"""
    setup_chain(db, "chainA", feeders["chainA"])
    setup_chain(db, "chainB", feeders["chainB"])
    push_chain(db, "chainA", range(0, 3), tip_height=2)   # LOCK@4 块头尚未登记；tip=2
    push_chain(db, "chainB", range(0, 5))
    lock = make_event(feeders["chainA"], chain_id="chainA", event_type="LOCK",
                      nonce="msg-x", source_chain="chainA", contract=CONTRACT,
                      amount=5, block_height=4)
    mint = make_event(feeders["chainB"], chain_id="chainB", event_type="MINT",
                      nonce="msg-x", source_chain="chainA", contract=CONTRACT,
                      amount=5, block_height=1)
    ingest(db, lock)
    ingest(db, mint)
    r = reconcile(db)
    assert r["findings"] == [], r["findings"]
    p = db.query(Pairing).filter_by(reconciliation_id=_rid(db, r)).one()
    assert p.state == "HALF_OPEN"


def test_timeout_unpaired_lock(db, feeders):
    """LOCK 确认后超过 message_timeout_seconds 仍无 MINT => 超时未配对证据。"""
    setup_chain(db, "chainA", feeders["chainA"], confirmation_depth=1, timeout=3600)
    setup_chain(db, "chainB", feeders["chainB"], confirmation_depth=1, timeout=3600)
    times = {h: BASE_TIME for h in range(0, 6)}  # 所有块时间固定
    push_chain(db, "chainA", range(0, 6), times=times)
    push_chain(db, "chainB", range(0, 6), times=times)
    lock = make_event(feeders["chainA"], chain_id="chainA", event_type="LOCK",
                      nonce="slow", source_chain="chainA", contract=CONTRACT,
                      amount=7, block_height=1)
    ingest(db, lock)

    # 未超时：half-open，不报
    early = reconcile(db, as_of=BASE_TIME + timedelta(seconds=100))
    assert early["findings"] == [], early["findings"]

    # 超时后：UNPAIRED_LOCK_TIMEOUT + 守恒失衡（锁了没铸）
    late = reconcile(db, as_of=BASE_TIME + timedelta(seconds=3601))
    codes = finding_codes(late)
    assert "UNPAIRED_LOCK_TIMEOUT" in codes
    assert "CONSERVATION_IMBALANCE" in codes
    path = evidence_by_code(late, "UNPAIRED_LOCK_TIMEOUT")
    doc = json.load(open(path))
    assert doc["evidence"]["lock"]["event_type"] == "LOCK"
    assert doc["evidence"]["timeout_seconds"] == 3600
    assert doc["run_id"] == late["run_id"]


def test_reorg_reverts_confirmed_event(db, feeders):
    """分叉撤销：事件先 CONFIRMED，更高分叉后 REORGED；快照历史保留，结论翻转。"""
    setup_chain(db, "chainA", feeders["chainA"], confirmation_depth=2)
    setup_chain(db, "chainB", feeders["chainB"], confirmation_depth=2)
    push_chain(db, "chainA", range(0, 5))
    push_chain(db, "chainB", range(0, 5))
    lock = make_event(feeders["chainA"], chain_id="chainA", event_type="LOCK",
                      nonce="reorg-1", source_chain="chainA", contract=CONTRACT,
                      amount=50, block_height=1)
    mint = make_event(feeders["chainB"], chain_id="chainB", event_type="MINT",
                      nonce="reorg-1", source_chain="chainA", contract=CONTRACT,
                      amount=50, block_height=1)
    ingest(db, lock)
    ingest(db, mint)
    r1 = reconcile(db)
    assert r1["findings"] == [], r1["findings"]
    assert db.get(ChainEvent, _event_id(db, lock)).status == "CONFIRMED"

    # 在 chainA 上从高度0之后分叉，建一条更长竞争链（5个块），使原高度1的 LOCK 块被甩出
    prev = block_hash("chainA", 0)
    fork_heights = list(range(1, 6))
    for h in fork_heights:
        fh = fork_hash("chainA", h, "x")
        chains_svc.upsert_block(
            db, "chainA", h, fh, prev, BASE_TIME + timedelta(seconds=90 + h)
        )
        prev = fh
    chains_svc.set_tip(db, "chainA", prev)

    r2 = reconcile(db)
    codes = finding_codes(r2)
    assert "REORGED_EVENT" in codes, codes
    assert "MINT_WITHOUT_LOCK" in codes  # LOCK 被撤销后 MINT 失去背书
    ev = db.get(ChainEvent, _event_id(db, lock))
    assert ev.status == "REORGED" and ev.prev_status == "CONFIRMED"

    # 证据文件真实存在并记录分叉事实
    doc = json.load(open(evidence_by_code(r2, "REORGED_EVENT")))
    assert doc["evidence"]["event"]["block_hash"] == block_hash("chainA", 1)
    assert doc["evidence"]["chain_id"] == "chainA"

    # 两次快照都保留，且哈希链接
    runs = db.query(Reconciliation).order_by(Reconciliation.id).all()
    assert len(runs) == 2
    assert runs[1].prev_snapshot_hash == runs[0].snapshot_hash
    snap1, snap2 = json.load(open(runs[0].snapshot_path)), json.load(open(runs[1].snapshot_path))
    assert snap1["snapshot_hash"] != snap2["snapshot_hash"]
    assert snap2["prev_snapshot_hash"] == snap1["snapshot_hash"]


def test_same_contract_address_two_chains(db, feeders):
    """两链相同合约地址：资产 UID 不同，互不配对、不撞账。"""
    same_addr = "0xSAMEADDR"
    setup_chain(db, "chainA", feeders["chainA"])
    setup_chain(db, "chainB", feeders["chainB"])
    push_chain(db, "chainA", range(0, 5))
    push_chain(db, "chainB", range(0, 5))

    # 资产 A 原生于 chainA（LOCK on A / MINT on B）
    lock_a = make_event(feeders["chainA"], chain_id="chainA", event_type="LOCK",
                        nonce="a1", source_chain="chainA", contract=same_addr,
                        amount=10, block_height=1)
    mint_a = make_event(feeders["chainB"], chain_id="chainB", event_type="MINT",
                        nonce="a1", source_chain="chainA", contract=same_addr,
                        amount=10, block_height=1)
    # 资产 B 原生于 chainB，用同一合约地址（LOCK on B / MINT on A）
    lock_b = make_event(feeders["chainB"], chain_id="chainB", event_type="LOCK",
                        nonce="b1", source_chain="chainB", contract=same_addr,
                        amount=20, block_height=2)
    mint_b = make_event(feeders["chainA"], chain_id="chainA", event_type="MINT",
                        nonce="b1", source_chain="chainB", contract=same_addr,
                        amount=20, block_height=2)
    for env in (lock_a, mint_a, lock_b, mint_b):
        ingest(db, env)

    r = reconcile(db)
    assert r["findings"] == [], r["findings"]
    pairs = db.query(Pairing).filter_by(reconciliation_id=_rid(db, r)).all()
    assert len(pairs) == 2
    assert pairs[0].asset_uid != pairs[1].asset_uid
    for p in pairs:
        assert p.state == "PAIRED"


def test_duplicate_delivery_is_idempotent(db, feeders):
    """重复投递（同一链上事件身份）不重复计数。"""
    setup_chain(db, "chainA", feeders["chainA"])
    setup_chain(db, "chainB", feeders["chainB"])
    push_chain(db, "chainA", range(0, 5))
    push_chain(db, "chainB", range(0, 5))
    lock = make_event(feeders["chainA"], chain_id="chainA", event_type="LOCK",
                      nonce="dup-delivery", source_chain="chainA",
                      contract=CONTRACT, amount=100, block_height=1)
    ingest(db, lock)
    ingest(db, lock, expect="duplicate")
    ingest(db, lock, expect="duplicate")  # 共投递 3 次
    mint = make_event(feeders["chainB"], chain_id="chainB", event_type="MINT",
                      nonce="dup-delivery", source_chain="chainA",
                      contract=CONTRACT, amount=100, block_height=1)
    ingest(db, mint)

    r = reconcile(db)
    assert r["findings"] == [], r["findings"]
    assert db.query(ChainEvent).filter(ChainEvent.event_type == "LOCK").count() == 1
    snap = json.load(open(r["snapshot_path"]))
    assert snap["delivery"]["distinct_events"] == 2
    assert snap["delivery"]["total_deliveries"] == 4  # 3 次 LOCK + 1 次 MINT


def test_duplicate_mint(db, feeders):
    """同一 LOCK 消息两次 MINT（第二个换 tx_hash/log_index）=> DUPLICATE_MINT 证据。"""
    setup_chain(db, "chainA", feeders["chainA"])
    setup_chain(db, "chainB", feeders["chainB"])
    push_chain(db, "chainA", range(0, 5))
    push_chain(db, "chainB", range(0, 5))
    lock = make_event(feeders["chainA"], chain_id="chainA", event_type="LOCK",
                      nonce="dm", source_chain="chainA", contract=CONTRACT,
                      amount=100, block_height=1)
    m1 = make_event(feeders["chainB"], chain_id="chainB", event_type="MINT",
                    nonce="dm", source_chain="chainA", contract=CONTRACT,
                    amount=100, block_height=1, log_index=0)
    m2 = make_event(feeders["chainB"], chain_id="chainB", event_type="MINT",
                    nonce="dm", source_chain="chainA", contract=CONTRACT,
                    amount=100, block_height=2, log_index=0, tx_suffix="#2")
    ingest(db, lock)
    ingest(db, m1)
    ingest(db, m2)
    r = reconcile(db)
    codes = finding_codes(r)
    assert "DUPLICATE_MINT" in codes
    assert "CONSERVATION_IMBALANCE" in codes  # 多铸 100
    doc = json.load(open(evidence_by_code(r, "DUPLICATE_MINT")))
    assert len(doc["evidence"]["all_mints"]) == 2
    assert doc["evidence"]["lock"]["amount"] == "100"


def test_mint_without_lock(db, feeders):
    """只有 MINT 没有任何 LOCK（连未确认的都没有）=> MINT_WITHOUT_LOCK 证据。"""
    setup_chain(db, "chainA", feeders["chainA"])
    setup_chain(db, "chainB", feeders["chainB"])
    push_chain(db, "chainA", range(0, 5))
    push_chain(db, "chainB", range(0, 5))
    mint = make_event(feeders["chainB"], chain_id="chainB", event_type="MINT",
                      nonce="phantom", source_chain="chainA", contract=CONTRACT,
                      amount=999, block_height=1)
    ingest(db, mint)
    r = reconcile(db)
    codes = finding_codes(r)
    assert "MINT_WITHOUT_LOCK" in codes
    doc = json.load(open(evidence_by_code(r, "MINT_WITHOUT_LOCK")))
    assert doc["evidence"]["mint"]["event_type"] == "MINT"
    assert doc["code"] == "MINT_WITHOUT_LOCK"


def test_amount_mismatch(db, feeders):
    """LOCK 100 / MINT 90 => AMOUNT_MISMATCH + 守恒失衡。"""
    setup_chain(db, "chainA", feeders["chainA"])
    setup_chain(db, "chainB", feeders["chainB"])
    push_chain(db, "chainA", range(0, 5))
    push_chain(db, "chainB", range(0, 5))
    lock = make_event(feeders["chainA"], chain_id="chainA", event_type="LOCK",
                      nonce="am", source_chain="chainA", contract=CONTRACT,
                      amount=100, block_height=1)
    mint = make_event(feeders["chainB"], chain_id="chainB", event_type="MINT",
                      nonce="am", source_chain="chainA", contract=CONTRACT,
                      amount=90, block_height=1)
    ingest(db, lock)
    ingest(db, mint)
    r = reconcile(db)
    codes = finding_codes(r)
    assert "AMOUNT_MISMATCH" in codes and "CONSERVATION_IMBALANCE" in codes
    doc = json.load(open(evidence_by_code(r, "AMOUNT_MISMATCH")))
    assert doc["evidence"]["difference"] == "10"


def test_message_asset_mismatch_and_burn_release(db, feeders):
    """BURN/RELEASE 正常往返；同 message_id 跨资产 => MESSAGE_ASSET_MISMATCH。"""
    setup_chain(db, "chainA", feeders["chainA"])
    setup_chain(db, "chainB", feeders["chainB"])
    push_chain(db, "chainA", range(0, 5))
    push_chain(db, "chainB", range(0, 5))
    # 正常 BURN(B) -> RELEASE(A)
    burn = make_event(feeders["chainB"], chain_id="chainB", event_type="BURN",
                      nonce="back-1", source_chain="chainA", contract=CONTRACT,
                      amount=40, block_height=1)
    release = make_event(feeders["chainA"], chain_id="chainA", event_type="RELEASE",
                         nonce="back-1", source_chain="chainA", contract=CONTRACT,
                         amount=40, block_height=2)
    # 同一 message_id 被复用到另一资产
    other_uid_event = make_event(feeders["chainA"], chain_id="chainA", event_type="LOCK",
                                 nonce="back-1", source_chain="chainA",
                                 contract="0xOTHER", amount=1, block_height=3,
                                 tx_suffix="#asset2")
    ingest(db, burn)
    ingest(db, release)
    ingest(db, other_uid_event)
    r = reconcile(db)
    codes = finding_codes(r)
    assert "MESSAGE_ASSET_MISMATCH" in codes, codes
    assert "EVENT_WRONG_CHAIN_SIDE" not in codes
    br = db.query(Pairing).filter_by(
        reconciliation_id=_rid(db, r), direction="BURN_RELEASE").one()
    assert br.state == "PAIRED" and br.burn_amount == 40


def test_invalid_signature_rejected(db, feeders):
    """签名必须真实校验：伪造签名与非登记公钥一律拒收。"""
    setup_chain(db, "chainA", feeders["chainA"])
    push_chain(db, "chainA", range(0, 5))
    env = make_event(feeders["chainA"], chain_id="chainA", event_type="LOCK",
                     nonce="sig", source_chain="chainA", contract=CONTRACT,
                     amount=1, block_height=1)
    # 篡改 payload 但保留原签名
    tampered = dict(env)
    tampered["payload"] = {**env["payload"], "amount": "999"}
    with pytest.raises(IngestError):
        ingest_event(db, tampered["payload"], tampered["signature_hex"],
                     tampered["feeder_public_key_hex"])
    # 非登记公钥
    from app.demo_crypto import DemoFeeder
    stranger = DemoFeeder()
    with pytest.raises(IngestError):
        ingest_event(db, env["payload"], env["signature_hex"], stranger.public_key)
    db.rollback()
    assert db.query(ChainEvent).count() == 0


def test_snapshots_chained_and_evidence_layout(db, feeders, dirs):
    """每次对账保留快照；快照内容哈希与哈希链可验证；证据按 run 分目录。"""
    lock, _mint = _happy_pair_setup(db, feeders, nonce="snap")
    ingest(db, lock)  # 仅 LOCK => 失衡 + 超时与否取决于 as_of
    r1 = reconcile(db)
    r2 = reconcile(db)

    for r in (r1, r2):
        body = json.load(open(r["snapshot_path"]))
        from app.crypto import canonical_json, sha256_hex
        stored = body.pop("snapshot_hash")
        assert sha256_hex(canonical_json(body)) == stored
    assert r2["prev_snapshot_hash"] == r1["snapshot_hash"]
    # 证据文件在各自 run 目录下
    assert r1["snapshot_path"].startswith(str(dirs["snapshots"]))
    for f in r1["findings"]:
        assert f["evidence_path"].startswith(str(dirs["evidence"] / r1["run_id"]))


# ---------------------------------------------------------------------------

def _rid(db, result) -> int:
    return db.query(Reconciliation).filter_by(run_id=result["run_id"]).one().id


def _event_id(db, envelope) -> int:
    from app.identity import validate_event_payload
    key = validate_event_payload(envelope["payload"])["event_key"]
    return db.query(ChainEvent).filter_by(event_key=key).one().id
