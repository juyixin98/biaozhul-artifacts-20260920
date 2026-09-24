"""On-chain acceptance tests.

Every on-chain lookup answer is cross-checked against the independent
linear-scan reference model in app.reference. Cases cover:
  1. empty records
  2. first block
  3. gaps between blocks
  4. many updates merged into a single block
"""
from __future__ import annotations

import pytest

from app.contract import FutureBlockError
from app.reference import ReferenceCheckpoints
from tests.conftest import batch_set_in_one_block, mine_empty_blocks

# Offsets (in blocks, relative to the first write's block) at which values
# are written. Gaps are deliberately irregular: adjacent blocks 5/6, a 1->5
# jump, a long 10->50 gap.
WRITE_OFFSETS = [0, 4, 5, 9, 49]
WRITE_VALUES = [10, 50, 60, 100, 500]


def _build_reference(first_block: int) -> ReferenceCheckpoints:
    ref = ReferenceCheckpoints()
    for offset, value in zip(WRITE_OFFSETS, WRITE_VALUES):
        ref.set_value(first_block + offset, value)
    return ref


def _assert_matches_chain(checkpoint, ref: ReferenceCheckpoints, target: int,
                          current: int) -> None:
    chain = checkpoint.get_at_block(target)
    lin = ref.linear_lookup(target, current)
    if lin is None:
        assert chain.exists is False, (target, chain)
        assert chain.value == 0 and chain.block_number == 0
    else:
        assert chain.exists is True, target
        assert chain.block_number == lin.block_number
        assert chain.value == lin.value


# ---------------------------------------------------------------- 1. empty

class TestEmpty:
    def test_length_zero(self, checkpoint):
        assert checkpoint.length() == 0

    def test_latest_empty(self, checkpoint):
        latest = checkpoint.latest()
        assert latest.exists is False
        assert latest.value == 0

    @pytest.mark.parametrize("target", [0, 1, 2, 100])
    def test_lookup_empty_any_past_block(self, checkpoint, w3, target):
        # Empty chain is at block 0; queries must not look into the future.
        if target > w3.eth.block_number:
            mine_empty_blocks(w3, target - w3.eth.block_number)
        result = checkpoint.get_at_block(target)
        assert result.exists is False
        assert result.value == 0

    def test_http_empty_history(self, client):
        health = client.get("/health").json()
        assert health["checkpoint_count"] == 0
        r = client.get("/values/latest")
        body = r.json()
        assert body["exists"] is False
        assert body["block_number"] is None
        assert body["value"] is None


# ---------------------------------------------------------------- 2. first

class TestFirstBlock:
    def test_first_block_exact_and_after_gap(self, checkpoint, w3):
        # Deployed around block 0/1; set in the current block, then jump.
        set_block = int(checkpoint.set_value(42)["blockNumber"])
        ref = ReferenceCheckpoints()
        ref.set_value(set_block, 42)

        mine_empty_blocks(w3, 7)
        current = w3.eth.block_number

        # Exact block.
        _assert_matches_chain(checkpoint, ref, set_block, current)
        # First block queried after a large gap.
        _assert_matches_chain(checkpoint, ref, current, current)
        # Target before the first checkpoint -> empty.
        before = set_block - 1
        if before >= 0:
            _assert_matches_chain(checkpoint, ref, before, current)

    def test_http_first_block(self, client):
        r = client.post("/values", json={"value": 777})
        assert r.status_code == 201
        body = r.json()
        first_block = body["block_number"]
        assert body["value"] == 777
        assert body["merged"] is False

        r = client.get(f"/values/at/{first_block}")
        assert r.status_code == 200
        assert r.json() == {
            "exists": True,
            "block_number": first_block,
            "value": 777,
        }


# ---------------------------------------------------------------- 3. gaps

class TestBlockGaps:
    def test_gaps_vs_linear_reference(self, checkpoint, w3):
        # First write at whatever block the fresh chain is currently at; all
        # further writes are expressed as offsets so the test is independent
        # of absolute chain height.
        first_receipt = checkpoint.set_value(WRITE_VALUES[0])
        first_block = int(first_receipt["blockNumber"])
        ref = _build_reference(first_block)
        write_blocks = [first_block + o for o in WRITE_OFFSETS]

        for block, value in list(zip(write_blocks, WRITE_VALUES))[1:]:
            head = w3.eth.block_number
            assert head < block, f"chain already at/past block {block} (at {head})"
            # Leave the target block for the transaction itself to fill.
            mine_empty_blocks(w3, block - head - 1)
            receipt = checkpoint.set_value(value)
            assert int(receipt["blockNumber"]) == block

        assert checkpoint.length() == len(WRITE_VALUES)
        current = w3.eth.block_number

        # Every boundary and gap interior.
        targets: set[int] = set()
        for block in write_blocks:
            targets.update({block - 1, block, block + 1, block + 2})
        # Additional interior points of the long gap.
        targets.update(
            {first_block + 7, first_block + 25, first_block + 48, current}
        )
        for target in sorted(t for t in targets if 0 <= t <= current):
            _assert_matches_chain(checkpoint, ref, target, current)

        # latest() reflects the final checkpoint.
        latest = checkpoint.latest()
        assert latest.exists is True
        assert latest.block_number == write_blocks[-1]
        assert latest.value == WRITE_VALUES[-1]

    def test_http_gap_queries(self, client, w3):
        # Write at current block, skip blocks, write again.
        b1 = client.post("/values", json={"value": 100}).json()["block_number"]
        mine_empty_blocks(w3, 9)
        b2 = client.post("/values", json={"value": 200}).json()["block_number"]
        assert b2 == b1 + 10

        # Gap interior returns the older value with its own block.
        for target in range(b1, b2):
            body = client.get(f"/values/at/{target}").json()
            assert body["exists"] is True
            assert body["block_number"] == b1
            assert body["value"] == 100

        body = client.get(f"/values/at/{b2}").json()
        assert body == {"exists": True, "block_number": b2, "value": 200}

        # Before the first checkpoint.
        if b1 > 0:
            body = client.get(f"/values/at/{b1 - 1}").json()
            assert body["exists"] is False


# ------------------------------------------------ 4. many updates one block

class TestManyUpdatesSameBlock:
    def test_batch_merges_to_one_checkpoint(self, checkpoint, w3):
        n = 300
        values = [i * 11 + 3 for i in range(1, n + 1)]
        block_number, receipts = batch_set_in_one_block(
            w3, checkpoint.address, values
        )

        # Exactly one checkpoint; last write wins.
        assert checkpoint.length() == 1
        bn, onchain_value = checkpoint.contract.functions.checkpointAt(0).call()
        assert int(bn) == block_number
        assert onchain_value == values[-1]

        # Reference model agrees (all updates merged).
        ref = ReferenceCheckpoints()
        for v in values:
            ref.set_value(block_number, v)
        _assert_matches_chain(checkpoint, ref, block_number, w3.eth.block_number)

        # Value persists across subsequent blocks.
        mine_empty_blocks(w3, 5)
        current = w3.eth.block_number
        _assert_matches_chain(checkpoint, ref, current, current)

        # All 300 transactions were mined successfully in that single block.
        assert len(receipts) == n
        assert len({int(r["blockNumber"]) for r in receipts}) == 1

    def test_http_batch_then_lookup(self, client, w3):
        from app.config import ANVIL_TEST_PRIVATE_KEYS
        from app.contract import load_abi

        # Use the same helper against the contract the HTTP layer is wired to.
        health = client.get("/health").json()
        contract_address = health["contract_address"]

        n = 200
        values = list(range(1000, 1000 + n))
        block_number, _ = batch_set_in_one_block(
            w3, contract_address, values
        )

        body = client.get("/values/latest").json()
        assert body == {
            "exists": True,
            "block_number": block_number,
            "value": values[-1],
        }
        assert client.get("/health").json()["checkpoint_count"] == 1

        body = client.get(f"/values/at/{block_number}").json()
        assert body["value"] == values[-1]


# ------------------------------------------------------- future block guard

class TestFutureBlock:
    def test_contract_reverts_on_future_block(self, checkpoint, w3):
        current = w3.eth.block_number
        with pytest.raises(FutureBlockError) as exc:
            checkpoint.get_at_block(current + 1)
        assert exc.value.requested == current + 1
        assert exc.value.current == current

    def test_http_rejects_future_block(self, client, w3):
        current = client.get("/health").json()["current_block"]
        r = client.get(f"/values/at/{current + 5}")
        assert r.status_code == 400
        detail = r.json()["detail"]
        assert detail["error"] == "future_block"
        assert detail["requested_block"] == current + 5
        assert detail["current_block"] == current

    def test_http_current_block_allowed(self, client):
        current = client.get("/health").json()["current_block"]
        r = client.get(f"/values/at/{current}")
        assert r.status_code == 200
