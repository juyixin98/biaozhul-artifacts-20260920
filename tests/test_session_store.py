"""Tests for late-data replay window and restart persistence."""
from __future__ import annotations

from pathlib import Path

from app.estimator import Sample
from app.session_store import (
    REASON_TOO_OLD_FOR_REPLAY,
    SessionStore,
)


def _sample(t: float) -> Sample:
    return Sample(float(t), 0.0, 3.8, 25.0)


def test_late_data_outside_replay_window_rejected(tmp_path, params):
    store = SessionStore(params, tmp_path)
    sess = store.create(replay_window_s=100.0)
    r1 = store.ingest(sess.session_id, [_sample(t) for t in range(0, 501)])
    assert r1.accepted == 501

    # Stream forward, leaving 501..899 unfilled.
    r1b = store.ingest(sess.session_id, [_sample(t) for t in range(900, 1001)])
    assert r1b.accepted == 101

    # A single forward sample pushes latest to 1100 (cutoff = 1000).
    r1c = store.ingest(sess.session_id, [_sample(1100.0)])
    assert r1c.accepted == 1

    # Two genuinely new late samples: t=750 is older than the cutoff ->
    # rejected; t=1050 is inside the replay window and previously
    # unknown -> accepted (bounded replay).
    r2 = store.ingest(sess.session_id, [_sample(750.0), _sample(1050.0)])
    assert r2.accepted == 1
    assert r2.rejected == 1
    assert r2.rejections[0]["reason"] == REASON_TOO_OLD_FOR_REPLAY
    assert len(store.get(sess.session_id).samples) == 604

    # An actual duplicate timestamp is also rejected.
    r3 = store.ingest(sess.session_id, [_sample(0.0)])
    assert r3.accepted == 0 and r3.rejected == 1


def test_state_survives_restart_and_finalize_blocks_ingest(tmp_path, params):
    data_dir = Path(tmp_path)
    store1 = SessionStore(params, data_dir)
    sess = store1.create(replay_window_s=60.0, initial_soc=0.8)
    store1.ingest(
        sess.session_id,
        [Sample(float(t), 1.0, 3.8, 25.0) for t in range(0, 120)],
    )
    sid = sess.session_id

    # Simulate a process restart: a fresh store over the same directory.
    store2 = SessionStore(params, data_dir)
    sess2 = store2.get(sid)
    assert sess2 is not None
    assert len(sess2.samples) == 120

    result = store2.finalize(sid)
    assert result.final_soc is not None
    assert store2.get(sid).finalized is True

    # Finalized sessions reject new data.
    report = store2.ingest(sid, [_sample(500.0)])
    assert report.accepted == 0 and report.rejected == 1
