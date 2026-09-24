"""Tests for SQLite persistence and the HMAC-SHA256 evidence chain."""
import json
import sqlite3

import pytest

from time_alignment.matcher import AlignmentMatcher
from time_alignment.model import AlignParams, StreamEvent
from time_alignment.storage import (
    EvidenceStore, canonical_payload, load_key, signed_body,
)
import hashlib
import hmac

KEY = b"0123456789abcdef0123456789abcdef"


def _populate(path, params=None):
    store = EvidenceStore(path, KEY)
    m = AlignmentMatcher(params or AlignParams())
    for ev in [StreamEvent("imu", 10_000_000, "i0"),
               StreamEvent("camera", 11_000_000, "c0"),
               StreamEvent("imu", 200_000_000, "tick"),
               StreamEvent("camera", 50_000_000, "clost_cam")]:
        store.append_many(m.register(ev).outcomes)
    store.append_many(m.finalize().outcomes)
    store.close()


def test_roundtrip_verify_valid(tmp_path):
    db = tmp_path / "e.db"
    _populate(db)
    with EvidenceStore(db, KEY) as store:
        report = store.verify()
    assert report.ok, report.first_error
    assert report.total >= 4
    assert report.counts["pair"] >= 1


def test_payload_tamper_detected(tmp_path):
    db = tmp_path / "e.db"
    _populate(db)
    conn = sqlite3.connect(db)
    row = conn.execute(
        "SELECT seq,payload FROM records WHERE kind='pair' LIMIT 1").fetchone()
    p = json.loads(row[1])
    p["status"] = "matched" if p["status"] != "matched" else "expired_unpaired"
    conn.execute("UPDATE records SET payload=? WHERE seq=?",
                 (canonical_payload(p), row[0]))
    conn.commit()
    conn.close()
    with EvidenceStore(db, KEY) as store:
        report = store.verify()
    assert not report.ok
    assert "digest mismatch" in report.first_error


def test_mac_chain_swap_detected(tmp_path):
    db = tmp_path / "e.db"
    _populate(db)
    conn = sqlite3.connect(db)
    a = conn.execute("SELECT mac FROM records WHERE seq=2").fetchone()[0]
    b = conn.execute("SELECT mac FROM records WHERE seq=3").fetchone()[0]
    conn.execute("UPDATE records SET mac=? WHERE seq=2", (b,))
    conn.execute("UPDATE records SET mac=? WHERE seq=3", (a,))
    conn.commit()
    conn.close()
    with EvidenceStore(db, KEY) as store:
        report = store.verify()
    assert not report.ok
    assert "HMAC chain broken" in report.first_error


def test_wrong_key_detected(tmp_path):
    db = tmp_path / "e.db"
    _populate(db)
    with EvidenceStore(db, b"f" * 32) as store:
        assert not store.verify().ok


def test_row_delete_detected(tmp_path):
    db = tmp_path / "e.db"
    _populate(db)
    conn = sqlite3.connect(db)
    conn.execute("DELETE FROM records WHERE seq=2")
    conn.commit()
    conn.close()
    with EvidenceStore(db, KEY) as store:
        report = store.verify()
    assert not report.ok and "sequence gap" in report.first_error


def test_math_is_real_hmac_sha256(tmp_path):
    """Independently recompute row 1's MAC with stdlib and compare bytes."""
    db = tmp_path / "e.db"
    _populate(db)
    conn = sqlite3.connect(db)
    row = conn.execute(
        "SELECT seq,kind,epoch,params_version,payload,mac FROM records "
        "ORDER BY seq LIMIT 1").fetchone()
    body = signed_body(row[0], row[1], row[2], row[3], row[4])
    digest = hashlib.sha256(body).hexdigest()
    expect = hmac.new(KEY, digest.encode("ascii"),
                      hashlib.sha256).hexdigest()
    assert hmac.compare_digest(expect, row[5])


def test_fetch_and_export_jsonl(tmp_path):
    db = tmp_path / "e.db"
    _populate(db)
    with EvidenceStore(db, KEY) as store:
        pairs = store.fetch_pairs()
        assert pairs and all("camera" in p for p in pairs)
        n = store.export_jsonl(tmp_path / "out.jsonl")
    lines = (tmp_path / "out.jsonl").read_text().strip().splitlines()
    assert len(lines) == n
    first = json.loads(lines[0])
    assert {"seq", "kind", "digest", "mac", "payload"} <= set(first)


def test_key_loading_and_min_length(tmp_path):
    k = tmp_path / "k.key"
    k.write_bytes(KEY.hex().encode() + b"\n")
    assert load_key(k) == KEY
    short = tmp_path / "short.key"
    short.write_bytes(b"short")
    with pytest.raises(ValueError):
        load_key(short)


def test_resume_appends_continue_chain(tmp_path):
    db = tmp_path / "e.db"
    s1 = EvidenceStore(db, KEY)
    m1 = AlignmentMatcher()
    s1.append_many(m1.register(StreamEvent("imu", 0, "i0")).outcomes)
    s1.close()
    s2 = EvidenceStore(db, KEY)
    m2 = AlignmentMatcher()
    s2.append_many(m2.register(StreamEvent("camera", 1_000_000, "c0")).outcomes)
    s2.append_many(m2.finalize().outcomes)
    report = s2.verify()
    s2.close()
    assert report.ok
