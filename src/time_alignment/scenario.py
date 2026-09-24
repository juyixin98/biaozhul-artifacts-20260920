"""Offline JSON scenarios: replay, expectation checking, evidence reports,
and a built-in synthetic generator (no ROS, no hardware needed).

Scenario JSON schema
--------------------
``{"name", "description", "config", "events", "expect"}``

``config`` (all times milliseconds)::

    {"tolerance_ms": 5, "imu_policy": "exclusive" | "reuse",
     "out_of_order_ms": 100, "reset_threshold_ms": 100,
     "camera_cache_max": 2048, "imu_cache_max": 8192}

``events`` are given in *arrival order* (so out-of-order delivery is
expressed by arrival order differing from ``stamp_ms``)::

    {"kind": "camera" | "imu", "stamp_ms": float, "recv_ms": float,
     "seq": int (optional), "frame_id": str (optional), "payload": object}

``expect`` (all fields optional)::

    {"epochs": int,
     "matches": [[camera_seq, imu_seq], ...],
     "camera_unmatched": [camera_seq, ...],
     "imu_unmatched": [imu_seq, ...],
     "tie_cameras": [camera_seq, ...],
     "status_counts": {"MATCHED": n, "CAMERA_UNMATCHED": n, "IMU_UNMATCHED": n},
     "chain_ok": true}

Payloads are hashed with SHA-256 for cryptographic pairing evidence, the
same way the live ROS node hashes real message bytes.
"""

from __future__ import annotations

import hashlib
import json
import os
from typing import Any

from .config import AlignConfig, EXCLUSIVE
from .engine import PairingEngine
from .storage import Storage
from .types import (
    CAMERA,
    IMU,
    NANOSECONDS_PER_MILLISECOND,
    NANOSECONDS_PER_SECOND,
    Status,
)

MS = NANOSECONDS_PER_MILLISECOND

_DEFAULT_CONFIG = {
    "tolerance_ms": 5.0,
    "imu_policy": EXCLUSIVE,
    "out_of_order_ms": 100.0,
    "reset_threshold_ms": 100.0,
    "camera_cache_max": 2048,
    "imu_cache_max": 8192,
}


def ms_to_ns(value: float) -> int:
    return int(round(float(value) * MS))


def payload_hash(payload: Any) -> str:
    """Real SHA-256 over canonical JSON of the event payload."""
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


# --------------------------------------------------------------- config
def config_from_dict(raw: dict[str, Any] | None, version: int = 1) -> AlignConfig:
    raw = {**_DEFAULT_CONFIG, **(raw or {})}
    cfg = AlignConfig(
        version=version,
        tolerance_ns=ms_to_ns(raw["tolerance_ms"]),
        imu_policy=str(raw["imu_policy"]),
        out_of_order_ns=ms_to_ns(raw["out_of_order_ms"]),
        reset_threshold_ns=ms_to_ns(raw["reset_threshold_ms"]),
        camera_cache_max=int(raw["camera_cache_max"]),
        imu_cache_max=int(raw["imu_cache_max"]),
    )
    return cfg


# --------------------------------------------------------------- replay
def run_scenario(scenario: dict[str, Any], db_path: str = ":memory:",
                 hmac_secret: bytes | str | None = None,
                 finalize: bool = True) -> dict[str, Any]:
    """Replay one scenario through the real engine + storage.

    Returns a result dict containing decisions, epochs, config versions and
    the cryptographic chain verification outcome.
    """
    cfg = config_from_dict(scenario.get("config"))
    storage = Storage(db_path, hmac_secret=hmac_secret)
    engine = PairingEngine(storage, cfg, initial_recv_ns=0)

    for ev in scenario.get("events", []):
        payload = ev.get("payload", {"v": ev.get("stamp_ms")})
        engine.add_event(
            kind=ev["kind"],
            stamp_ns=ms_to_ns(ev["stamp_ms"]),
            recv_ns=ms_to_ns(ev["recv_ms"]),
            seq=ev.get("seq"),
            payload_hash=payload_hash(payload),
            frame_id=ev.get("frame_id", "cam0"),
            payload=payload,
        )
    if finalize:
        last_recv = max((ms_to_ns(e["recv_ms"]) for e in scenario.get("events", [])),
                        default=0)
        engine.finalize(recv_ns=last_recv)

    decisions = storage.all_decisions()
    epochs = storage.list_epochs()
    configs = storage.list_configs()
    chain = storage.verify_chain()
    summary = {
        "name": scenario.get("name", ""),
        "description": scenario.get("description", ""),
        "decisions": decisions,
        "epochs": epochs,
        "config_versions": configs,
        "chain": chain,
        "snapshot": engine.snapshot(),
    }
    summary["status_counts"] = _status_counts(decisions)
    storage.close()
    return summary


def _status_counts(decisions: list[dict[str, Any]]) -> dict[str, int]:
    counts = {s.value: 0 for s in Status}
    for d in decisions:
        counts[d["status"]] = counts.get(d["status"], 0) + 1
    return counts


def format_evidence(summary: dict[str, Any], limit: int | None = None) -> str:
    """Human-readable pairing-evidence table (for CLI / CI logs)."""
    lines = []
    lines.append(f"scenario : {summary['name']}")
    lines.append(f"epochs   : {len(summary['epochs'])}  "
                 f"config versions: {[c['version'] for c in summary['config_versions']]}")
    lines.append(f"chain    : {summary['chain']['algorithm']} "
                 f"verified={summary['chain']['ok']} rows={summary['chain']['rows']}")
    lines.append(f"counts   : {summary['status_counts']}")
    header = (f"{'row':>4} {'ep':>2} {'cfg':>3} {'status':<17} "
              f"{'camSeq':>6} {'camT(ms)':>9} {'imuSeq':>6} {'imuT(ms)':>9} "
              f"{'dt(ms)':>8} {'tie':>3} {'reason'}")
    lines.append(header)
    lines.append("-" * len(header))
    rows = summary["decisions"] if limit is None else summary["decisions"][:limit]
    for d in rows:
        lines.append(
            f"{d['seq'] if 'seq' in d else 0:>4} "
            f"{d['epoch_id']:>2} {d['config_version']:>3} {d['status']:<17} "
            f"{_fmt(d['camera_seq'], 6)} {_fmtms(d['camera_stamp_ns']):>9} "
            f"{_fmt(d['imu_seq'], 6)} {_fmtms(d['imu_stamp_ns']):>9} "
            f"{_fmtms(d['dt_ns'], sign=True):>8} {('Y' if d['tie'] else ''):>3} "
            f"{d['reason']}"
        )
    if limit is not None and len(summary["decisions"]) > limit:
        lines.append(f"... ({len(summary['decisions']) - limit} more rows)")
    return "\n".join(lines)


def _fmt(value: Any, width: int) -> str:
    return ("" if value is None else str(value)).rjust(width)


def _fmtms(value_ns: int | None, sign: bool = False) -> str:
    if value_ns is None:
        return ""
    v = value_ns / MS
    if sign:
        return f"{v:+.3f}"
    return f"{v:.3f}"


# ---------------------------------------------------------- expectations
def check_expectations(summary: dict[str, Any], scenario: dict[str, Any]) -> list[str]:
    """Return a list of human-readable failures; empty list = all passed."""
    failures: list[str] = []
    expect = scenario.get("expect", {})
    decisions = summary["decisions"]

    if "epochs" in expect:
        # Epoch ids start at 0; number of opened epochs = len(list).
        actual = len(summary["epochs"])
        if actual != expect["epochs"]:
            failures.append(f"epochs: expected {expect['epochs']}, got {actual}")

    matched = {(d["camera_seq"]): d for d in decisions if d["status"] == Status.MATCHED.value}
    cam_un = {d["camera_seq"] for d in decisions
              if d["status"] == Status.CAMERA_UNMATCHED.value}
    imu_un = {d["imu_seq"] for d in decisions
              if d["status"] == Status.IMU_UNMATCHED.value}

    for pair in expect.get("matches", []):
        cam_seq, imu_seq = pair[0], pair[1]
        d = matched.get(cam_seq)
        if d is None:
            failures.append(f"matches: camera_seq {cam_seq} expected to match "
                            f"imu_seq {imu_seq}, but is not MATCHED")
        elif d["imu_seq"] != imu_seq:
            failures.append(f"matches: camera_seq {cam_seq} matched imu_seq "
                            f"{d['imu_seq']}, expected {imu_seq}")
        elif len(pair) >= 3:
            dt_ms = d["dt_ns"] / MS
            if abs(dt_ms - float(pair[2])) > 1e-6:
                failures.append(f"matches: camera_seq {cam_seq} dt {dt_ms}ms, "
                                f"expected {pair[2]}ms")

    for cam_seq in expect.get("camera_unmatched", []):
        if cam_seq not in cam_un:
            failures.append(f"camera_unmatched: camera_seq {cam_seq} was expected "
                            "to be CAMERA_UNMATCHED")

    for imu_seq in expect.get("imu_unmatched", []):
        if imu_seq not in imu_un:
            failures.append(f"imu_unmatched: imu_seq {imu_seq} was expected "
                            "to be IMU_UNMATCHED")

    for cam_seq in expect.get("tie_cameras", []):
        d = matched.get(cam_seq)
        if d is None:
            failures.append(f"tie_cameras: camera_seq {cam_seq} not matched at all")
        elif not d["tie"]:
            failures.append(f"tie_cameras: camera_seq {cam_seq} tie flag not set")

    if "status_counts" in expect:
        for key, val in expect["status_counts"].items():
            if summary["status_counts"].get(key, 0) != val:
                failures.append(
                    f"status_counts[{key}]: expected {val}, got "
                    f"{summary['status_counts'].get(key, 0)}")

    if expect.get("chain_ok", False) and not summary["chain"]["ok"]:
        failures.append("chain_ok: cryptographic chain did not verify")

    return failures


# --------------------------------------------------- synthetic generator
def _e(kind: str, stamp_ms: float, recv_ms: float | None = None,
       seq: int | None = None, payload: Any | None = None,
       frame_id: str = "cam0") -> dict[str, Any]:
    return {
        "kind": kind,
        "stamp_ms": stamp_ms,
        "recv_ms": stamp_ms if recv_ms is None else recv_ms,
        **({"seq": seq} if seq is not None else {}),
        "frame_id": frame_id,
        "payload": payload if payload is not None else {
            "kind": kind, "stamp_ms": stamp_ms,
            "seq": seq if seq is not None else -1,
        },
    }


def _c(t: float, recv_ms: float | None = None, seq: int | None = None) -> dict[str, Any]:
    return _e(CAMERA, t, recv_ms, seq)


def _i(t: float, recv_ms: float | None = None, seq: int | None = None) -> dict[str, Any]:
    return _e(IMU, t, recv_ms, seq)


def _gen_burst() -> dict[str, Any]:
    """Dense burst of frames near 250ms; exclusive IMU policy means only the
    earliest frame can claim the nearby IMU sample."""
    events = [_i(t, seq=k) for k, t in enumerate(range(0, 460, 10))]
    events += [_c(t, seq=k) for k, t in enumerate(range(0, 250, 50))]
    # 4 frames burst in at ~260 ms arrival time
    events += [_c(t, recv_ms=260, seq=100 + k) for k, t in enumerate([252, 253, 254, 255])]
    events += [_c(300, seq=20)]
    events = sorted(events, key=lambda e: e["recv_ms"])
    return {
        "name": "burst",
        "description": "Sudden camera burst under exclusive IMU policy.",
        "config": {"tolerance_ms": 5, "imu_policy": "exclusive"},
        "events": events,
        "expect": {
            "epochs": 1,
            "matches": [[0, 0], [1, 5], [2, 10], [3, 15], [4, 20],
                        [100, 25], [103, 26], [20, 30]],
            "camera_unmatched": [101, 102],
            "chain_ok": True,
        },
    }


def _gen_drops() -> dict[str, Any]:
    """IMU samples at 40/50/60ms are lost; frames at 45/55 must expire."""
    imu_times = [t for t in range(0, 130, 10) if t not in (40, 50, 60)]
    events = [_i(t, seq=k) for k, t in enumerate(imu_times)]
    cam_times = list(range(0, 120, 10))
    events += [_c(t, seq=k) for k, t in enumerate(cam_times)]
    events = sorted(events, key=lambda e: (e["recv_ms"], 0 if e["kind"] == IMU else 1))
    # IMUs: 0,10,20,30,70,80,90,100,110,120 -> seqs 0..9
    return {
        "name": "drops",
        "description": "IMU packet loss: frames in the gap expire unmatched.",
        "config": {"tolerance_ms": 5, "imu_policy": "exclusive"},
        "events": events,
        "expect": {
            "epochs": 1,
            "matches": [[0, 0], [1, 1], [2, 2], [3, 3],
                        [7, 4], [8, 5], [9, 6], [10, 7], [11, 8]],
            "camera_unmatched": [4, 5, 6],
            "imu_unmatched": [9],
            "chain_ok": True,
        },
    }


def _gen_identical() -> dict[str, Any]:
    """Tie rules: identical IMU stamps -> lowest seq; symmetric equidistant
    pair -> earlier IMU time wins."""
    events = [
        _i(0, seq=0), _c(0, seq=0),
        _i(100, seq=10), _i(100, seq=11), _c(100, seq=1),
        _i(195, seq=12), _i(205, seq=13), _c(200, seq=2),
        _i(310, seq=14), _c(310, seq=3),
    ]
    return {
        "name": "identical_timestamps",
        "description": "Duplicate IMU stamps and equidistant tie: earlier wins.",
        "config": {"tolerance_ms": 5, "imu_policy": "exclusive"},
        "events": events,
        "expect": {
            "epochs": 1,
            "matches": [[0, 0], [1, 10], [2, 12], [3, 14]],
            "tie_cameras": [1, 2],
            # seq 11 duplicates stamp 100ms; seq 13 is the +5ms tie loser at
            # 200ms. Under the exclusive policy both remain unconsumed.
            "imu_unmatched": [11, 13],
            "chain_ok": True,
        },
    }


def _gen_reset() -> dict[str, Any]:
    """Clock reset at the camera: timestamps jump backwards >100ms, opening
    a new epoch; nothing pairs across the boundary."""
    events = [
        _i(0, seq=0), _c(0, seq=0),
        _i(100, seq=1), _c(100, seq=1),
        _i(200, seq=2), _c(200, seq=2),
        # backwards jump: camera clock resets to ~50 ms
        _c(50, recv_ms=250, seq=50),
        _i(50, recv_ms=251, seq=50),
        _c(55, recv_ms=255, seq=51),
        _i(55, recv_ms=256, seq=51),
        # tail drains the new epoch
        _i(200, recv_ms=300, seq=60), _c(200, recv_ms=301, seq=60),
    ]
    return {
        "name": "clock_reset",
        "description": "Clock reset opens epoch 1; no cross-epoch pairing.",
        "config": {"tolerance_ms": 5, "imu_policy": "exclusive",
                   "reset_threshold_ms": 100},
        "events": events,
        "expect": {
            "epochs": 2,
            "matches": [[0, 0], [1, 1], [2, 2], [50, 50], [51, 51], [60, 60]],
            "chain_ok": True,
        },
    }


def _gen_out_of_order() -> dict[str, Any]:
    """Late IMU arrivals: 92ms late is still integrated (frame matches it);
    a 120ms-late IMU arrives after settlement and expires unused."""
    events = [
        _i(0, seq=0), _c(0, seq=0),
        _i(100, seq=1), _c(100, seq=1),
        _i(200, seq=2), _c(200, seq=2),
        _i(300, seq=3), _c(300, seq=3),
        # late but within the 100ms disorder window for camera 200:
        _i(198, recv_ms=290, seq=90),
        # too late: camera 300 has already settled by the time this arrives
        _i(302, recv_ms=420, seq=91),
        _i(410, recv_ms=410, seq=4), _c(410, recv_ms=411, seq=4),
    ]
    return {
        "name": "out_of_order",
        "description": "Late arrivals within 100ms are honored; a 120ms-late "
                       "IMU lags the high-water mark past the reset threshold "
                       "and opens a new epoch instead.",
        "config": {"tolerance_ms": 5, "imu_policy": "exclusive",
                   "out_of_order_ms": 100},
        "events": sorted(events, key=lambda e: e["recv_ms"]),
        "expect": {
            "epochs": 2,
            # camera 200 prefers the exact 200ms IMU over the late 198ms one;
            # the late 198ms IMU is consumed by nothing and expires.
            "matches": [[0, 0], [1, 1], [2, 2], [3, 3], [4, 4]],
            "imu_unmatched": [90, 91],
            "chain_ok": True,
        },
    }


def _gen_param_atom() -> dict[str, Any]:
    """Tolerance widening at a boundary (config v2) changes later matches;
    earlier decisions stay stamped with v1."""
    # Constructed via run_scenario-with-staging? JSON can't stage params;
    # this scenario keeps static params. Param atomicity is covered in tests
    # and by the live node; still provide a loose-tolerance static scenario.
    events = [_i(t, seq=k) for k, t in enumerate(range(0, 200, 10))]
    events += [_c(35, seq=0), _c(95, seq=1)]
    return {
        "name": "loose_tolerance",
        "description": "20ms tolerance: frames 5ms from the nearest IMU match.",
        "config": {"tolerance_ms": 20, "imu_policy": "exclusive"},
        "events": sorted(events, key=lambda e: (e["recv_ms"], 0 if e["kind"] == IMU else 1)),
        "expect": {
            "epochs": 1,
            # frame 35ms is equidistant from the 30/40ms IMUs: earlier wins.
            "matches": [[0, 3], [1, 9]],
            "chain_ok": True,
        },
    }


GENERATORS = {
    "burst": _gen_burst,
    "drops": _gen_drops,
    "identical_timestamps": _gen_identical,
    "clock_reset": _gen_reset,
    "out_of_order": _gen_out_of_order,
    "loose_tolerance": _gen_param_atom,
}


def generate_named(name: str) -> dict[str, Any]:
    if name not in GENERATORS:
        raise KeyError(f"unknown scenario {name!r}; choose from {sorted(GENERATORS)}")
    return GENERATORS[name]()


def write_example_scenarios(directory: str) -> list[str]:
    os.makedirs(directory, exist_ok=True)
    paths = []
    for name in sorted(GENERATORS):
        scenario = GENERATORS[name]()
        path = os.path.join(directory, f"{name}.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(scenario, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
        paths.append(path)
    return paths


def load_scenario(path: str) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


# Time unit re-export for callers building custom scenarios.
SECOND_NS = NANOSECONDS_PER_SECOND
