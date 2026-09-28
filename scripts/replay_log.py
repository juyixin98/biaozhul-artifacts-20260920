#!/usr/bin/env python3
"""Pretty-print / sanity-check a replayable JSONL run log.

Usage::

    python3 scripts/replay_log.py logs/demo-diamond.jsonl

It verifies every event carries run id + monotonic seq, reconstructs the
per-wave resident-memory timeline and the slot timeline, and lists failures
with their stable categories -- the information needed to reproduce a
problem without the original process.
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path


def load(path: Path) -> list[dict]:
    events = []
    with path.open(encoding="utf-8") as fh:
        for line_no, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            events.append(json.loads(line))
    return events


def replay(events: list[dict]) -> int:
    problems: list[str] = []
    run_ids = sorted({e["run_id"] for e in events})
    print(f"runs in log ({len(run_ids)}): {run_ids}")
    print(f"events: {len(events)}")

    seqs: dict[str, list[int]] = defaultdict(list)
    for e in events:
        seqs[e["run_id"]].append(e["seq"])
    for rid, seq_list in seqs.items():
        if seq_list != list(range(1, len(seq_list) + 1)):
            problems.append(f"{rid}: seq not strictly monotonic from 1")

    failures = [e for e in events if e["kind"] == "failure"]
    print(f"\nfailures: {len(failures)}")
    for e in failures:
        d = e["data"]
        print(f"  run={e['run_id']} seq={e['seq']} "
              f"category={d.get('category')!r}: {e['message']}")
        for key in ("node", "required", "capacity", "charged_peak_bytes",
                    "budget_bytes", "over_bytes", "feed", "expected", "got"):
            if key in d:
                print(f"      {key} = {d[key]}")

    print("\nper-run timeline:")
    for rid in run_ids:
        run_events = [e for e in events if e["run_id"] == rid]
        print(f"\n  [{rid}]")
        for e in run_events:
            d = e["data"]
            extra = []
            if e["kind"] in ("run_start",):
                extra.append(f"feeds={d.get('feed_shapes')}")
                extra.append(f"budget={d.get('budget')}")
                extra.append(f"retained={d.get('retained_output_bytes')}")
            if e["kind"] == "capacity_deficit":
                extra.append(f"deficits={d.get('deficits')}")
            if e["kind"] == "replan":
                extra.append(f"{d.get('old_peak_resident')} -> "
                             f"{d.get('new_peak_resident')} bytes")
            if e["kind"] == "wave_end":
                extra.append(
                    f"wave={d.get('wave')} pool={d.get('pool_bytes')} "
                    f"ext={d.get('external_bytes')} "
                    f"resident={d.get('resident_bytes')}"
                )
            if e["kind"] == "node_done":
                extra.append(
                    f"{d.get('node')}:{d.get('op')} "
                    f"in={d.get('input_shapes')} out={d.get('output_shapes')}"
                )
            if e["kind"] in ("outputs_retained", "outputs_released"):
                extra.append(f"retained_bytes={d.get('retained_bytes')}")
            suffix = (" | " + "; ".join(extra)) if extra else ""
            print(f"    {e['seq']:>3} {e['kind']:<18} {e['message']}{suffix}")

    wave_ends = [e for e in events if e["kind"] == "wave_end"]
    if wave_ends:
        peak = max(e["data"]["resident_bytes"] for e in wave_ends)
        print(f"\nobserved resident peak from log: {peak} bytes")

    if problems:
        print("\nLOG INTEGRITY PROBLEMS:")
        for p in problems:
            print(f"  - {p}")
        return 1
    print("\nlog integrity OK")
    return 0


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__.strip())
        return 2
    path = Path(argv[1])
    if not path.exists():
        print(f"no such log: {path}")
        return 2
    return replay(load(path))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
