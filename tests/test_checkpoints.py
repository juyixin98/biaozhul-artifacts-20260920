"""Checkpoint save / restart-continue / source-change rejection."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from tests.conftest import requires_ros


def _manager(tmp_path, monkeypatch):
    from app.config import Settings
    from app.crypto import load_or_create_secret
    from app.sessions import SessionManager

    bag_root = tmp_path / "bags"
    bag_root.mkdir(parents=True, exist_ok=True)
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    monkeypatch.setenv("REPLAY_BAG_ROOTS", str(bag_root))
    monkeypatch.setenv("REPLAY_STATE_DIR", str(state))
    monkeypatch.setenv("REPLAY_HMAC_KEY", "checkpoint-test-secret-0123456789")
    settings = Settings()
    key = load_or_create_secret(settings.secret_file)
    return SessionManager(settings, key), settings, bag_root, key


@requires_ros
def test_checkpoint_restarts_and_continues_from_position(tmp_path, monkeypatch):
    """Restart-continue: play a bit, checkpoint, restore, next seq matches."""
    from bagtools.bagmaker import make_bag

    manager, settings, bag_root, key = _manager(tmp_path, monkeypatch)
    bag = make_bag(bag_root / "cp", messages=40, gap_ns=20_000_000, same_time_groups=1)

    s1 = manager.create(bag["uri"], rate=50.0)
    s1.engine.play()
    # wait until we've passed message 15
    deadline = __import__("time").time() + 5
    while s1.engine.position()["next_seq"] < 16 and __import__("time").time() < deadline:
        __import__("time").sleep(0.01)
    s1.engine.pause()
    saved = manager.save_checkpoint(s1, pause=True)
    cp_next = saved["payload"]["position"]["next_seq"]
    assert 16 <= cp_next <= 40
    envelope_path = saved["path"]

    # Simulate full service restart: throw away all session state.
    manager.shutdown_all()
    manager2, _, _, _ = _manager(tmp_path, monkeypatch)
    with open(envelope_path, "r", encoding="utf-8") as fh:
        envelope = json.load(fh)

    s2 = manager2.restore(envelope)
    # restored session is paused at the saved playhead
    assert s2.engine.position()["playing"] is False
    assert s2.engine.position()["next_seq"] == cp_next
    assert s2.engine.position()["generation"] >= 1  # restore opens a generation

    # Continue: must deliver the remaining messages exactly once.
    s2.engine.play()
    import time
    deadline = time.time() + 5
    while not s2.engine.position()["finished"] and time.time() < deadline:
        time.sleep(0.01)
    assert s2.engine.position()["finished"]

    new_gen = s2.engine.position()["generation"]
    msgs = [
        i
        for i in s2.sink.history()
        if i.get("kind") == "message" and i["generation"] == new_gen
    ]
    expected_seqs = list(range(cp_next, 41))
    delivered_seqs = [m["seq"] for m in msgs]
    assert delivered_seqs == expected_seqs

    # Overall (pre-checkpoint + post-restore) the whole bag was delivered
    # exactly once.
    pre = [
        i for i in s1.sink.history() if i.get("kind") == "message"
    ]
    pre_before = [m["seq"] for m in pre if m["seq"] < cp_next]
    assert sorted(pre_before) == list(range(1, cp_next))
    manager2.shutdown_all()


@requires_ros
def test_restore_rejects_tampered_signature(tmp_path, monkeypatch):
    from bagtools.bagmaker import make_bag

    manager, _, bag_root, _ = _manager(tmp_path, monkeypatch)
    bag = make_bag(bag_root / "sig", messages=10)
    s = manager.create(bag["uri"])
    saved = manager.save_checkpoint(s)
    envelope = json.loads(Path(saved["path"]).read_text())
    envelope["checkpoint"]["position"]["next_seq"] = 99

    from app.crypto import CheckpointSignatureError

    with pytest.raises(CheckpointSignatureError):
        manager.restore(envelope)
    manager.shutdown_all()


@requires_ros
def test_restore_rejects_when_storage_file_changed(tmp_path, monkeypatch):
    from bagtools.bagmaker import make_bag

    manager, _, bag_root, _ = _manager(tmp_path, monkeypatch)
    bag = make_bag(bag_root / "changed", messages=12)
    s = manager.create(bag["uri"])
    saved = manager.save_checkpoint(s)
    envelope = json.loads(Path(saved["path"]).read_text())
    manager.shutdown_all()

    # Source changed on disk: append a byte to the storage file.
    mcap = next(Path(bag["uri"]).glob("*.mcap"))
    with mcap.open("ab") as fh:
        fh.write(b"\x00")

    from app.checkpoints import SourceChangedError

    with pytest.raises(SourceChangedError):
        manager.restore(envelope)


@requires_ros
def test_restore_rejects_corrupted_bag(tmp_path, monkeypatch):
    from bagtools.bagmaker import make_bag, make_corrupt

    manager, _, bag_root, _ = _manager(tmp_path, monkeypatch)
    bag_dir = bag_root / "corrupt_later"
    make_bag(bag_dir, messages=12)
    s = manager.create(str(bag_dir))
    saved = manager.save_checkpoint(s)
    envelope = json.loads(Path(saved["path"]).read_text())
    manager.shutdown_all()

    # Truncate the storage file after checkpoint was sealed.
    mcap = next(bag_dir.glob("*.mcap"))
    mcap.open("r+b").truncate(mcap.stat().st_size // 2)

    from app.bagstore import CorruptBagError
    from app.checkpoints import SourceChangedError

    with pytest.raises((CorruptBagError, SourceChangedError)):
        manager.restore(envelope)


@requires_ros
def test_restore_rejects_missing_bag(tmp_path, monkeypatch):
    import shutil

    from bagtools.bagmaker import make_bag

    manager, _, bag_root, _ = _manager(tmp_path, monkeypatch)
    bag = make_bag(bag_root / "gone", messages=8)
    s = manager.create(bag["uri"])
    saved = manager.save_checkpoint(s)
    envelope = json.loads(Path(saved["path"]).read_text())
    manager.shutdown_all()

    shutil.rmtree(bag["uri"])
    with pytest.raises(Exception):
        manager.restore(envelope)
