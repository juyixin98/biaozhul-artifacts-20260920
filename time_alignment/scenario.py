"""Scenario JSON loader and strict acceptance evaluator (no ROS required).

A scenario drives the matcher purely from data, so every edge case (bursts,
packet loss, equal timestamps, clock resets, reordering, parameter changes)
is reproducible without hardware.

Event objects::

    {"kind": "camera"|"imu", "id": "c0", "t": 0.012}      # seconds (float)
        also accepted: "t_ns" (int), "recv"/"recv_ns",
                       "epoch_hint", "payload"
    {"kind": "params", "version": 2, "params": { ... AlignParams fields ... }}
    {"kind": "reset", "id": "manual-reset"}

``expect`` (all optional)::

    {"epochs": 3,
     "pairs": [{"camera": "c0", "status": "matched", "imu": "i0",
                "dt_ms": -2.0, "tie_break": "earlier",
                "params_version": 1, "epoch": 1}],
     "imus":  [{"imu": "i3", "status": "unused_expired", "use_count": 0}],
     "params_versions": [1, 2]}
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .matcher import AlignmentMatcher, MatchResult
from .model import AlignParams, StreamEvent


def _ns(spec: dict, key: str, key_ns: str) -> Optional[int]:
    if key_ns in spec:
        return int(spec[key_ns])
    if key in spec:
        return int(round(float(spec[key]) * 1_000_000_000))
    return None


def load_scenario(path: str | Path) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if "events" not in data or not isinstance(data["events"], list):
        raise ValueError(f"{path}: scenario requires an 'events' list")
    return data


def build_matcher(scenario: dict) -> AlignmentMatcher:
    p = AlignParams()
    if scenario.get("params"):
        p = AlignParams.from_dict({**p.to_dict(), **scenario["params"],
                                   "version": scenario["params"].get("version", 1)})
    return AlignmentMatcher(p)


def events_of(scenario: dict) -> list[StreamEvent]:
    out: list[StreamEvent] = []
    for i, spec in enumerate(scenario["events"]):
        kind = spec["kind"]
        if kind == "params":
            d = AlignParams().to_dict()
            d.update(spec["params"])
            d["version"] = int(spec["version"])
            out.append(_ParamsEvent(d))
            continue
        if kind == "reset":
            out.append(StreamEvent("reset", None,
                                   source_id=spec.get("id", f"reset-{i}"),
                                   payload=spec.get("payload", {})))
            continue
        t = _ns(spec, "t", "t_ns")
        recv = _ns(spec, "recv", "recv_ns")
        out.append(StreamEvent(
            kind, t,
            source_id=str(spec.get("id", f"{kind}-{i}")),
            recv_ns=recv,
            epoch_hint=spec.get("epoch_hint"),
            clock_generation=int(spec.get("clock_generation", 0)),
            payload=spec.get("payload", {})))
    return out


class _ParamsEvent:
    """Internal pseudo-event: parameter swap at this point in receive order."""

    def __init__(self, params_dict: dict) -> None:
        self.params_dict = params_dict


def run_scenario(scenario: dict) -> tuple[AlignmentMatcher, MatchResult]:
    m = build_matcher(scenario)
    all_out = []
    for ev in events_of(scenario):
        if isinstance(ev, _ParamsEvent):
            m.request_params(AlignParams.from_dict(ev.params_dict))
        else:
            all_out.extend(m.register(ev).outcomes)
    all_out.extend(m.finalize().outcomes)
    return m, MatchResult(all_out)


@dataclass
class Evaluation:
    ok: bool
    failures: list[str] = field(default_factory=list)
    pairs: list[dict] = field(default_factory=list)
    imus: list[dict] = field(default_factory=list)
    epochs: list[dict] = field(default_factory=list)
    params_versions: list[int] = field(default_factory=list)
    totals: dict[str, int] = field(default_factory=dict)

    def render(self) -> str:
        lines = []
        for p in self.pairs:
            mark = "✓" if p["status"] == "matched" else "✗"
            imu = p["imu"]["id"] if p.get("imu") else "-"
            dt = f"{p['dt_ns']/1e6:+.3f}ms" if p.get("dt_ns") is not None else "  -   "
            tie = f" tie={p['tie_break']}" if p.get("tie_break") else ""
            lines.append(
                f"  {mark} cam={p['camera']['id']:<6} ep={p.get('epoch')} "
                f"ver={p.get('params_version')} -> imu={imu:<6} {dt:>10} "
                f"{p['status']}{tie}")
        for i in self.imus:
            lines.append(
                f"    imu={i['imu']['id']:<6} ep={i.get('epoch')} "
                f"{i['status']} uses={i['use_count']}")
        lines.append(f"  epochs={len(self.epochs)} "
                     f"matched={self.totals['matched']} "
                     f"unpaired={self.totals['unpaired']} "
                     f"imu_records={len(self.imus)}")
        if self.failures:
            lines.append("  EXPECTATION FAILURES:")
            lines.extend(f"    - {x}" for x in self.failures)
        return "\n".join(lines)


def evaluate(scenario: dict) -> Evaluation:
    m, result = run_scenario(scenario)
    pairs = result.pairs
    imus = result.imus
    epochs = result.epochs
    versions = [p["version"] for p in result.params_changes]
    ev = Evaluation(
        ok=True,
        pairs=pairs, imus=imus, epochs=epochs, params_versions=versions,
        totals={"matched": sum(p["status"] == "matched" for p in pairs),
                "unpaired": sum(p["status"] != "matched" for p in pairs)})
    expect = scenario.get("expect", {})

    if "epochs" in expect and len([e for e in epochs if e.get("event") == "open"]) != int(expect["epochs"]):
        ev.failures.append(
            f"epochs: expected {expect['epochs']} opens, got "
            f"{len([e for e in epochs if e.get('event') == 'open'])}")

    by_cam = {p["camera"]["id"]: p for p in pairs}
    for want in expect.get("pairs", []):
        cid = want["camera"]
        got = by_cam.get(cid)
        if got is None:
            ev.failures.append(f"pair[{cid}]: no camera outcome emitted")
            continue
        if got["status"] != want["status"]:
            ev.failures.append(
                f"pair[{cid}]: status {got['status']!r} != "
                f"{want['status']!r}")
        want_imu = want.get("imu", ...)
        if want_imu is not ...:
            got_imu = got["imu"]["id"] if got.get("imu") else None
            if got_imu != want_imu:
                ev.failures.append(
                    f"pair[{cid}]: imu {got_imu!r} != {want_imu!r}")
        if "dt_ms" in want:
            got_dt = None if got["dt_ns"] is None else got["dt_ns"] / 1e6
            if got_dt is None or abs(got_dt - want["dt_ms"]) > 1e-6:
                ev.failures.append(
                    f"pair[{cid}]: dt_ms {got_dt} != {want['dt_ms']}")
        if "dt_ns" in want and got["dt_ns"] != want["dt_ns"]:
            ev.failures.append(
                f"pair[{cid}]: dt_ns {got['dt_ns']} != {want['dt_ns']}")
        if want.get("tie_break") is not None and got["tie_break"] != want["tie_break"]:
            ev.failures.append(
                f"pair[{cid}]: tie_break {got['tie_break']!r} != "
                f"{want['tie_break']!r}")
        if "params_version" in want and got["params_version"] != want["params_version"]:
            ev.failures.append(
                f"pair[{cid}]: params_version {got['params_version']} != "
                f"{want['params_version']}")
        if "epoch" in want and got["epoch"] != want["epoch"]:
            ev.failures.append(
                f"pair[{cid}]: epoch {got['epoch']} != {want['epoch']}")

    by_imu = {p["imu"]["id"]: p for p in imus}
    for want in expect.get("imus", []):
        iid = want["imu"]
        got = by_imu.get(iid)
        if got is None:
            ev.failures.append(f"imu[{iid}]: no IMU outcome emitted")
            continue
        if "status" in want and got["status"] != want["status"]:
            ev.failures.append(
                f"imu[{iid}]: status {got['status']!r} != {want['status']!r}")
        if "use_count" in want and got["use_count"] != want["use_count"]:
            ev.failures.append(
                f"imu[{iid}]: use_count {got['use_count']} != "
                f"{want['use_count']}")

    if "params_versions" in expect and versions != list(expect["params_versions"]):
        ev.failures.append(
            f"params versions: {versions} != {expect['params_versions']}")

    ev.ok = not ev.failures
    return ev
