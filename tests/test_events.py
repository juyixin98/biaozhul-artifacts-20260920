"""Event log: identity correlation, versions, verdict basis, honest status."""

import json

import numpy as np

from amptrain.events import EventLog

from tests.helpers import amplified_batches, normal_batches


def test_events_correlate_run_id_window_and_version(trainer):
    trainer.process_window(normal_batches(trainer.config, 1, seed=1))
    events = trainer.log.to_list()
    assert events, "a commit must be logged"
    commit = next(e for e in events if e["event"] == "window_committed")
    assert commit["run_id"] == trainer.run_id
    assert commit["window_index"] == 1
    assert commit["version"]["amptrain"]
    assert commit["version"]["numpy"]
    assert commit["detail"]["basis"] == "all_stages_finite"
    assert commit["detail"]["lr"] >= 0


def test_skip_event_records_stage_scale_and_decision(trainer):
    trainer.process_window(amplified_batches(trainer.config, 1000.0, 1, seed=2))
    skip = next(e for e in trainer.log.to_list() if e["event"] == "window_skipped")
    assert skip["run_id"] == trainer.run_id
    assert skip["detail"]["overflow_stage"] == "scaled_loss"
    assert skip["detail"]["scale_before"] == 128.0
    assert skip["detail"]["scale_after"] == 64.0
    assert skip["detail"]["decision"] == "drop_window_no_partial_commit_backoff_scale"
    # A skip is recorded as a skip -- never as a success event.
    assert all(e["event"] != "window_committed" or e["window_index"] != skip["window_index"]
               for e in trainer.log.to_list())


def test_jsonl_sink_writes_machine_readable_records(trainer, tmp_path):
    sink = tmp_path / "events.jsonl"
    trainer.log = EventLog(trainer.run_id, sink=sink)
    trainer.process_window(normal_batches(trainer.config, 1, seed=3))
    lines = sink.read_text().strip().splitlines()
    record = json.loads(lines[0])
    assert record["run_id"] == trainer.run_id
    assert record["event"] == "window_committed"
    assert "version" in record


def test_json_default_serializes_numpy_scalars_arrays_and_dtypes():
    from amptrain.events import _json_default

    assert _json_default(np.float32(1.5)) == 1.5
    assert _json_default(np.int64(7)) == 7
    assert _json_default(np.array([[1, 2], [3, 4]])) == [[1, 2], [3, 4]]
    assert _json_default(np.dtype(np.float16)) == "float16"


def test_json_default_rejects_unserializable_type():
    import pytest
    from amptrain.events import _json_default
    with pytest.raises(TypeError):
        _json_default(object())


def test_jsonl_sink_serializes_ndarray_and_dtype_details(tmp_path):
    sink = tmp_path / "rich.jsonl"
    log = EventLog("rich-run", sink=sink)
    log.append(
        "validation",
        {"sample_grad": np.array([1.0, 2.0]), "dtype": np.dtype(np.float16), "scale": np.float32(64.0)},
        window_index=0,
    )
    record = json.loads(sink.read_text().strip())
    assert record["detail"]["sample_grad"] == [1.0, 2.0]
    assert record["detail"]["dtype"] == "float16"
    assert record["detail"]["scale"] == 64.0


def test_events_returns_a_copy_of_the_list(trainer):
    trainer.process_window(normal_batches(trainer.config, 1, seed=9))
    snap = trainer.log.events()
    assert len(snap) >= 1
    # mutating the returned list must not corrupt the log internals
    snap.clear()
    assert len(trainer.log.events()) >= 1
