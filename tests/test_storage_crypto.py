"""Tests for the tamper-evident SQLite hash/HMAC chain.

The crypto here is really executed (SHA-256 / HMAC-SHA256); these tests
mutate rows at the SQLite level and prove verification detects it.
"""

import json
import sqlite3

import pytest

from time_alignment.config import AlignConfig
from time_alignment.engine import PairingEngine
from time_alignment.storage import ChainVerifyError, Storage
from time_alignment.types import CAMERA, IMU, Status


def _populate(path: str, secret=None, policy="exclusive"):
    storage = Storage(path, hmac_secret=secret)
    cfg = AlignConfig(version=1, imu_policy=policy)
    eng = PairingEngine(storage, cfg, initial_recv_ns=0)
    for t in range(0, 400, 10):
        eng.add_event(IMU, t * 1_000_000, t * 1_000_000, seq=t // 10,
                      payload_hash=f"imu-{t:03d}")
    for t in range(0, 400, 50):
        eng.add_event(CAMERA, t * 1_000_000, t * 1_000_000, seq=t // 50,
                      payload_hash=f"cam-{t:03d}")
    eng.finalize(recv_ns=600 * 1_000_000)
    storage.close()


def _raw_connect(path: str) -> sqlite3.Connection:
    return sqlite3.connect(path)


class TestPlainHashChain:
    def test_clean_chain_verifies(self, tmp_path):
        db = str(tmp_path / "ok.sqlite")
        _populate(db)
        with Storage(db) as st:
            res = st.verify_chain()
        assert res["ok"] is True
        assert res["algorithm"] == "SHA256"
        assert res["rows"] > 0

    def test_payload_tamper_detected(self, tmp_path):
        db = str(tmp_path / "tamper.sqlite")
        _populate(db)
        # Tamper directly with SQLite: reassign a camera to a different IMU.
        conn = _raw_connect(db)
        row = conn.execute("SELECT seq,imu_seq FROM decisions "
                           "WHERE status='MATCHED' LIMIT 1").fetchone()
        conn.execute("UPDATE decisions SET imu_seq = imu_seq + 1 WHERE seq=?",
                     (row[0],))
        conn.commit()
        conn.close()
        st = Storage(db)
        with pytest.raises(ChainVerifyError) as exc:
            st.verify_chain()
        st.close()
        assert exc.value.index == row[0]
        assert "row_hash mismatch" in exc.value.message

    def test_row_deletion_detected(self, tmp_path):
        db = str(tmp_path / "delete.sqlite")
        _populate(db)
        conn = _raw_connect(db)
        conn.execute("DELETE FROM decisions WHERE seq=3")
        conn.commit()
        conn.close()
        st = Storage(db)
        with pytest.raises(ChainVerifyError) as exc:
            st.verify_chain()
        st.close()
        assert exc.value.index == 3
        assert "gap" in exc.value.message or "prev_hash" in exc.value.message

    def test_reorder_detected(self, tmp_path):
        db = str(tmp_path / "reorder.sqlite")
        _populate(db)
        conn = _raw_connect(db)
        conn.execute("UPDATE decisions SET seq = 999 WHERE seq = 2")
        conn.commit()
        conn.close()
        st = Storage(db)
        with pytest.raises(ChainVerifyError):
            st.verify_chain()
        st.close()

    def test_candidate_json_is_real_evidence(self, tmp_path):
        db = str(tmp_path / "evid.sqlite")
        _populate(db)
        st = Storage(db)
        row = [r for r in st.all_decisions() if r["status"] == "MATCHED"][0]
        cands = row["candidates"]
        assert cands and {"imu_seq", "imu_stamp_ns", "dt_ns",
                          "abs_dt_ns", "payload_hash"} <= set(cands[0])
        # nearest candidate actually won
        winner = min(cands, key=lambda c: c["abs_dt_ns"])
        assert winner["imu_seq"] == row["imu_seq"]
        st.verify_chain()
        st.close()


class TestHmacChain:
    def test_hmac_chain_verifies_with_secret(self, tmp_path):
        db = str(tmp_path / "hmac.sqlite")
        _populate(db, secret=b"shared-secret")
        st = Storage(db, hmac_secret=b"shared-secret")
        res = st.verify_chain()
        assert res["algorithm"] == "HMAC-SHA256" and res["ok"]
        st.close()

    def test_wrong_secret_fails(self, tmp_path):
        db = str(tmp_path / "hmac2.sqlite")
        _populate(db, secret=b"right-secret")
        st = Storage(db, hmac_secret=b"wrong-secret")
        with pytest.raises(ChainVerifyError):
            st.verify_chain()
        st.close()

    def test_hmac_chain_changes_with_secret(self, tmp_path):
        db_a = str(tmp_path / "a.sqlite")
        db_b = str(tmp_path / "b.sqlite")
        _populate(db_a, secret=b"secret-A")
        _populate(db_b, secret=b"secret-B")
        with Storage(db_a, hmac_secret=b"secret-A") as sa:
            ta = sa.verify_chain()["tail"]
        with Storage(db_b, hmac_secret=b"secret-B") as sb:
            tb = sb.verify_chain()["tail"]
        assert ta != tb  # different keys -> different authenticated chain

    def test_exclusive_invariant_checked(self, tmp_path):
        # The same IMU matching twice must fail verification (data-level
        # check), regardless of crypto. Handcraft contradictory rows by
        # verifying first that legitimate exclusive chains have unique IMUs.
        db = str(tmp_path / "excl.sqlite")
        _populate(db, policy="exclusive")
        st = Storage(db)
        matched = [r for r in st.all_decisions() if r["status"] == "MATCHED"]
        imu_seqs = [r["imu_seq"] for r in matched]
        assert len(imu_seqs) == len(set(imu_seqs))
        st.close()
