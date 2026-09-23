"""测试构造辅助：链、块头、已签名事件、投递、对账。"""
from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone

from app import chains as chains_svc
from app import reconcile as recon_svc
from app.demo_crypto import DemoFeeder
from app.ingest import ingest_event

ZERO = "0x" + "0" * 64
BASE_TIME = datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc)


def block_hash(chain_id: str, height: int) -> str:
    tag = chain_id.encode().hex()[:8]
    return "0x" + f"{height:08x}{tag}".ljust(64, "0")


def fork_hash(chain_id: str, height: int, fork_name: str) -> str:
    return "0x" + hashlib.sha256(
        f"fork:{fork_name}:{chain_id}:{height}".encode()
    ).hexdigest()[:64]


def tx_hash(chain_id: str, nonce: str) -> str:
    return "0x" + hashlib.sha256(f"{chain_id}:{nonce}".encode()).hexdigest()[:64]


def setup_chain(
    db,
    chain_id: str,
    feeder: DemoFeeder,
    *,
    confirmation_depth: int = 2,
    timeout: int = 3600,
):
    chains_svc.register_chain(db, chain_id, chain_id, confirmation_depth, timeout)
    chains_svc.register_feeder(db, chain_id, feeder.public_key, feeder.label)


def push_chain(db, chain_id: str, heights, *, tip_height=None, times=None):
    """登记一条连续链（父块哈希按高度衔接），可指定 tip 高度。"""
    heights = sorted(heights)
    for h in heights:
        bh = block_hash(chain_id, h)
        parent = ZERO if h == 0 else block_hash(chain_id, h - 1)
        bt = (times or {}).get(h) or (BASE_TIME + timedelta(seconds=12 * h))
        chains_svc.upsert_block(db, chain_id, h, bh, parent, bt)
    if tip_height is None:
        tip_height = heights[-1]
    chains_svc.set_tip(db, chain_id, block_hash(chain_id, tip_height))


def make_event(
    feeder: DemoFeeder,
    *,
    chain_id: str,
    event_type: str,
    nonce: str,
    source_chain: str,
    contract: str,
    amount: int | str,
    block_height: int,
    token_id: str = "",
    log_index: int = 0,
    tx_suffix: str = "",
    account: str = "0xabc",
    block_hash_override: str | None = None,
):
    return feeder.event(
        chain_id=chain_id,
        event_type=event_type,
        tx_hash=tx_hash(chain_id, nonce + tx_suffix),
        log_index=log_index,
        block_height=block_height,
        block_hash=block_hash_override or block_hash(chain_id, block_height),
        source_chain=source_chain,
        original_contract=contract,
        source_nonce=nonce,
        account=account,
        amount=amount,
        token_id=token_id,
    )


def ingest(db, envelope, expect: str = "accepted"):
    res = ingest_event(
        db, envelope["payload"], envelope["signature_hex"],
        envelope["feeder_public_key_hex"],
    )
    assert res["status"] == expect, res
    return res


def reconcile(db, as_of=None):
    return recon_svc.run_reconciliation(
        db, as_of=as_of or (BASE_TIME + timedelta(seconds=600))
    )


def finding_codes(result) -> list[str]:
    return sorted(f["code"] for f in result["findings"])


def evidence_by_code(result, code: str) -> str:
    for f in result["findings"]:
        if f["code"] == code:
            return f["evidence_path"]
    raise KeyError(code)
