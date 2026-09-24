"""End-to-end verification tests at the chain/verifier level.

Covers the acceptance matrix:
  modify / delete / reorder a covered record -> TAMPERED
  forged checkpoint (bad signature)         -> TAMPERED
  truncate past the trusted checkpoint      -> TRUNCATED
  clean tail beyond the latest anchor       -> UNDETERMINED
  external anchor detects rollback/truncation
  untouched log anchored at head            -> VALID
"""

from __future__ import annotations

import copy

import pytest

from app import chain, keys as keymod
from app.canonical import GENESIS_HASH
from app.store import AuditStore
from app.verifier import (
    TAMPERED,
    TRUNCATED,
    UNDETERMINED,
    VALID,
    verify_bundle,
)


@pytest.fixture()
def store(tmp_path):
    key = keymod.generate_private_key()
    s = AuditStore(tmp_path / "data", key)
    s.ensure_genesis_anchor()
    return s


def _fill(store, n=6):
    for i in range(1, n + 1):
        store.append(
            actor=f"user{i}", action=f"act{i}", resource=f"/r{i}",
            payload={"n": i},
        )
    store.create_checkpoint()  # covers seq=6
    return store.export()


# --------------------------------------------------------------------- happy
def test_genesis_anchor_on_empty_log_is_valid(store):
    bundle = store.export()
    anchor = keymod.public_key_hex(store.signing_key)
    res = verify_bundle(bundle, anchor)
    assert res.status == VALID
    assert res.proven_upto == 0


def test_full_log_with_head_checkpoint_is_valid(store):
    bundle = _fill(store)
    res = verify_bundle(store.export(), keymod.public_key_hex(store.signing_key))
    assert res.status == VALID
    assert res.proven_upto == 6
    assert res.errors == []


def test_persistence_roundtrip(tmp_path):
    key = keymod.generate_private_key()
    d = tmp_path / "data"
    s1 = AuditStore(d, key)
    s1.ensure_genesis_anchor()
    s1.append(actor="a", action="x", resource="", payload=None)
    s1.create_checkpoint()
    s2 = AuditStore(d, key)
    res = verify_bundle(s2.export(), keymod.public_key_hex(key))
    assert res.status == VALID
    assert s2.length == 1


# --------------------------------------------------------------- tampering
def test_modify_covered_record_detected(store):
    _fill(store)
    bundle = store.export()
    bundle["records"][2]["payload"] = {"n": 999}  # seq 3 altered
    res = verify_bundle(bundle, keymod.public_key_hex(store.signing_key))
    assert res.status == TAMPERED
    assert any("recomputed hash" in e for e in res.errors)


def test_modify_actor_field_detected(store):
    _fill(store)
    bundle = store.export()
    bundle["records"][0]["actor"] = "mallory"
    # attacker also recomputes self-hash to defeat the trivial check
    bundle["records"][0]["hash"] = chain.record_hash(bundle["records"][0])
    res = verify_bundle(bundle, keymod.public_key_hex(store.signing_key))
    assert res.status == TAMPERED
    assert any("prev_hash mismatch" in e or "disagrees" in e for e in res.errors)


def test_delete_covered_record_detected(store):
    _fill(store)
    bundle = store.export()
    del bundle["records"][1]  # remove seq 2 -> gap
    res = verify_bundle(bundle, keymod.public_key_hex(store.signing_key))
    assert res.status == TAMPERED
    assert any("gap" in e or "prev_hash" in e for e in res.errors)


def test_reorder_records_detected(store):
    _fill(store)
    bundle = store.export()
    bundle["records"][0], bundle["records"][1] = (
        bundle["records"][1],
        bundle["records"][0],
    )
    res = verify_bundle(bundle, keymod.public_key_hex(store.signing_key))
    assert res.status == TAMPERED


def test_reorder_checkpoints_detected(store):
    _fill(store, 2)
    store.create_checkpoint()
    store.append(actor="x", action="y", resource="", payload=None)
    store.create_checkpoint()
    bundle = store.export()
    bundle["checkpoints"].reverse()  # same keys, bad order
    res = verify_bundle(bundle, keymod.public_key_hex(store.signing_key))
    assert res.status == TAMPERED
    assert any("prev_checkpoint_hash" in e for e in res.errors)


def test_forged_checkpoint_signature_detected(store):
    _fill(store)
    bundle = store.export()
    forged = copy.deepcopy(bundle["checkpoints"][-1])
    forged["record_hash"] = "a" * 64  # claims a different head
    # signature still the old one -> invalid
    bundle["checkpoints"].append(forged)
    res = verify_bundle(bundle, keymod.public_key_hex(store.signing_key))
    assert res.status == TAMPERED
    assert any("forged checkpoint" in e for e in res.errors)


def test_checkpoint_signed_by_stranger_key_detected(store):
    _fill(store)
    bundle = store.export()
    stranger = keymod.generate_private_key()
    fake = chain.build_checkpoint(
        seq=6,
        record_hash=bundle["records"][-1]["hash"],
        signing_key=stranger,
    )
    bundle["checkpoints"].append(fake)
    res = verify_bundle(bundle, keymod.public_key_hex(store.signing_key))
    assert res.status == TAMPERED
    assert any("forged checkpoint" in e for e in res.errors)


def test_checkpoint_disagreeing_with_record_detected(store):
    _fill(store)
    bundle = store.export()
    # Mis-signed head: valid Ed25519 signature by the real key over a body
    # whose record_hash does not match the recomputed chain. (A malicious
    # server in possession of the key could produce this; the cross-check
    # against records still catches it.)
    bogus = chain.build_checkpoint(
        seq=6, record_hash="b" * 64, signing_key=store.signing_key,
        prev_checkpoint_hash=chain.checkpoint_hash(bundle["checkpoints"][-1]),
    )
    bundle["checkpoints"].append(bogus)
    res = verify_bundle(bundle, keymod.public_key_hex(store.signing_key))
    assert res.status == TAMPERED
    assert any(
        "disagrees" in e or "conflicting trusted checkpoints" in e
        for e in res.errors
    )


def test_rehash_tail_rebuild_without_new_signature_is_truncated(store):
    _fill(store)
    bundle = store.export()
    # Attacker deletes seq 4..6 and rebuilds hashes for a self-consistent
    # seq4 replacement - but cannot update the signed checkpoint at seq 6.
    kept = bundle["records"][:3]
    fake4 = chain.build_record(
        seq=4, prev_hash=kept[-1]["hash"], actor="x", action="y",
        resource="", payload="evil",
    )
    bundle["records"] = kept + [fake4]
    res = verify_bundle(bundle, keymod.public_key_hex(store.signing_key))
    assert res.status == TRUNCATED


# --------------------------------------------------------------- truncation
def test_truncation_after_trusted_checkpoint_detected(store):
    _fill(store)
    bundle = store.export()
    bundle["records"] = bundle["records"][:4]  # checkpoint at seq 6, only 4 given
    res = verify_bundle(bundle, keymod.public_key_hex(store.signing_key))
    assert res.status == TRUNCATED
    assert any("seq=6" in e for e in res.errors)


def test_truncation_detected_by_external_anchor(store, tmp_path):
    _fill(store)
    full = store.export()
    anchor_hex = keymod.public_key_hex(store.signing_key)
    # Verifier caches the seq=6 checkpoint out-of-band (e.g. previous audit).
    cached = full["checkpoints"][-1]

    # Attacker later rewrites the server from a wiped directory using a
    # stranger key: shorter log, fresh-looking checkpoint list.
    evil = AuditStore(tmp_path / "evil-data", keymod.generate_private_key())
    evil.ensure_genesis_anchor()
    evil.append(actor="a", action="shorter", resource="", payload=None)
    evil.create_checkpoint()
    rolled = evil.export()
    # Verifier pins the ORIGINAL anchor key and cached checkpoint:
    res = verify_bundle(rolled, anchor_hex, external_anchor=cached)
    assert res.status == TAMPERED  # stranger-signed checkpoints fail;
    # external seq=6 vs empty-ish prefix would also imply truncation, but the
    # forged signatures take priority as hard failures.


def test_external_anchor_detects_simple_truncation(store):
    _fill(store)
    cached = store.export()["checkpoints"][-1]
    # Server (still holding the key) simply stops exporting records 5-6.
    bundle = store.export()
    bundle["records"] = bundle["records"][:4]
    res = verify_bundle(
        bundle, keymod.public_key_hex(store.signing_key),
        external_anchor=cached,
    )
    assert res.status == TRUNCATED


# ------------------------------------------------------------ undetermined
def test_clean_unanchored_tail_is_undetermined(store):
    _fill(store, 4)       # checkpoint covers seq 4
    store.create_checkpoint()
    store.append(actor="a", action="new", resource="", payload=None)
    bundle = store.export()
    res = verify_bundle(bundle, keymod.public_key_hex(store.signing_key))
    assert res.status == UNDETERMINED
    assert res.proven_upto == 4
    assert res.slice_end == 5


def test_modification_inside_unanchored_tail_still_detected(store):
    _fill(store, 4)
    store.create_checkpoint()
    store.append(actor="a", action="new", resource="", payload=None)
    bundle = store.export()
    bundle["records"][-1]["payload"] = "changed"
    res = verify_bundle(bundle, keymod.public_key_hex(store.signing_key))
    assert res.status == TAMPERED


def test_tail_undecided_but_new_checkpoint_resolves(store):
    _fill(store, 4)
    store.create_checkpoint()
    store.append(actor="a", action="new", resource="", payload=None)
    anchor = keymod.public_key_hex(store.signing_key)
    assert verify_bundle(store.export(), anchor).status == UNDETERMINED
    store.create_checkpoint()
    assert verify_bundle(store.export(), anchor).status == VALID


def test_empty_export_without_genesis_anchor_undetermined(store):
    # Build a bundle that a malicious empty server might serve: no records and
    # no anchors at all.
    bundle = {"records": [], "checkpoints": []}
    res = verify_bundle(bundle, keymod.public_key_hex(store.signing_key))
    assert res.status == UNDETERMINED


# ----------------------------------------------------------- bounded slices
def test_interior_slice_anchored_by_checkpoint(store):
    _fill(store)
    # checkpoint at seq 6 only; create one at seq 3 to anchor a slice 4..6
    store.checkpoints.clear()
    store.checkpoints_path.write_text("")
    key = store.signing_key
    # rebuild anchors at 0,3,6 deterministically
    from app.canonical import GENESIS_CHECKPOINT_HASH

    store.records  # noqa: B018 - ensure loaded
    cps = []
    prev = GENESIS_CHECKPOINT_HASH
    for seq in (0, 3, 6):
        rh = GENESIS_HASH if seq == 0 else store.records[seq - 1]["hash"]
        cp = chain.build_checkpoint(
            seq=seq, record_hash=rh, signing_key=key,
            prev_checkpoint_hash=prev,
        )
        cps.append(cp)
        prev = chain.checkpoint_hash(cp)
    store.checkpoints = cps
    bundle = store.export(start=4, end=6)
    res = verify_bundle(
        bundle, keymod.public_key_hex(key), expect_tail=False
    )
    assert res.status == VALID
    assert res.proven_upto == 6


def test_unanchored_interior_slice_is_undetermined(store):
    _fill(store)  # only checkpoint at seq 6 (plus genesis)
    bundle = store.export(start=2, end=4)
    res = verify_bundle(
        bundle, keymod.public_key_hex(store.signing_key), expect_tail=False
    )
    assert res.status == UNDETERMINED


def test_tail_slice_with_checkpoint_past_end_expect_tail_is_truncation(store):
    _fill(store)
    bundle = store.export(start=1, end=3)
    # Default expect_tail=True: asking "give me everything" but ending at 3
    # while an anchor covers 6 => truncation.
    res = verify_bundle(bundle, keymod.public_key_hex(store.signing_key))
    assert res.status == TRUNCATED


def test_wrong_anchor_key_rejects_all_checkpoints(store):
    _fill(store)
    other = keymod.public_key_hex(keymod.generate_private_key())
    res = verify_bundle(store.export(), other)
    assert res.status == TAMPERED
