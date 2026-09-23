"""Tests for checkpoint save/restore and source-change rejection."""
from __future__ import annotations

import json
import time

from rosreplay.checkpoints import CheckpointError, CheckpointStore


def test_save_and_load_roundtrip(manager):
    session = manager.create_session("demo", auto_play=False)
    cp = manager.save_checkpoint(session.id, "cp1")
    assert cp.checkpoint_id == "cp1"
    loaded = manager.store.load("cp1")
    assert loaded.bag["uri"].endswith("demo")
    assert loaded.position["next_seq"] == 0
    assert "metadata.yaml" in loaded.bag["files"]


def test_restore_continues_at_position(manager, helpers):
    session = manager.create_session("demo", rate=50.0, auto_play=True)
    helpers.wait_until(
        lambda: len(session.ring.snapshot()) >= 10, timeout=3
    )
    session.engine.pause()
    cp = manager.save_checkpoint(session.id, "resume_here")
    saved_seq = cp.position["next_seq"]
    assert saved_seq is not None and saved_seq >= 10

    # Simulate a service restart with a brand-new manager backed by the same
    # checkpoint directory.
    manager2 = type(manager)(manager.settings)
    try:
        restored = manager2.restore_checkpoint("resume_here", auto_play=False)
        st = restored.engine.status()
        assert st["next_seq"] == saved_seq
        restored.engine.set_rate(5000.0)
        restored.engine.resume()
        helpers.wait_until(lambda: st["state"] == "finished", timeout=1)
        helpers.wait_until(
            lambda: restored.engine.status()["state"] == "finished", timeout=3
        )
        records = restored.ring.snapshot()
        # Resumed session publishes only messages from the saved position.
        assert min(r.seq for r in records) == saved_seq
        assert [r.seq for r in records] == list(
            range(saved_seq, len(restored.index))
        )
    finally:
        manager2.shutdown_all()


def test_restore_rejects_modified_bag(manager, tmp_bag_root, helpers):
    session = manager.create_session("demo", auto_play=False)
    manager.save_checkpoint(session.id, "cp_tamper")

    meta = tmp_bag_root / "demo" / "metadata.yaml"
    with open(meta, "a", encoding="utf-8") as fh:
        fh.write("# changed after checkpoint\n")
    # Bypass index cache by using a fresh manager so the bag is re-read.
    manager2 = type(manager)(manager.settings)
    try:
        try:
            manager2.restore_checkpoint("cp_tamper")
            assert False, "expected restore to be rejected"
        except CheckpointError as exc:
            assert "source bag changed" in str(exc)
    finally:
        manager2.shutdown_all()


def test_restore_rejects_missing_file(manager, tmp_bag_root):
    session = manager.create_session("demo", auto_play=False)
    manager.save_checkpoint(session.id, "cp_missing")
    mcap = next((tmp_bag_root / "demo").glob("*.mcap"))
    mcap.unlink()
    manager2 = type(manager)(manager.settings)
    try:
        try:
            manager2.restore_checkpoint("cp_missing")
            assert False
        except CheckpointError as exc:
            assert "changed" in str(exc) or "removed" in str(exc)
    finally:
        manager2.shutdown_all()


def test_restore_topic_filter_persisted(manager, helpers):
    session = manager.create_session(
        "demo", topics=["/tick"], rate=30.0, auto_play=True
    )
    # Save mid-stream (paused) so restore has filtered messages left to publish.
    helpers.wait_until(
        lambda: len(session.ring.snapshot()) >= 3, timeout=3
    )
    session.engine.pause()
    manager.save_checkpoint(session.id, "cp_tickonly")
    manager2 = type(manager)(manager.settings)
    try:
        restored = manager2.restore_checkpoint("cp_tickonly", auto_play=True)
        restored.engine.set_rate(5000.0)
        helpers.wait_until(
            lambda: restored.engine.status()["state"] == "finished", 3
        )
        topics = {r.topic for r in restored.ring.snapshot()}
        assert topics == {"/tick"}, topics
    finally:
        manager2.shutdown_all()


def test_corrupt_checkpoint_file_rejected(manager):
    session = manager.create_session("demo", auto_play=False)
    manager.save_checkpoint(session.id, "cp_bad")
    path = manager.settings.checkpoint_dir / "cp_bad.json"
    path.write_text("{not valid json", encoding="utf-8")
    try:
        manager.store.load("cp_bad")
        assert False
    except CheckpointError:
        pass


def test_atomic_write_leaves_no_temp_files(manager):
    session = manager.create_session("demo", auto_play=False)
    manager.save_checkpoint(session.id, "cp_atomic")
    leftovers = list(manager.settings.checkpoint_dir.glob(".tmp-*"))
    assert leftovers == []
