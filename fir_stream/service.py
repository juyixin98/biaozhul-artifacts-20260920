"""Offline job runner: synth/PCM input -> streamed FIR processing -> PCM
output plus a JSON report.  No player, no UI — numbers and files only.

Job schema (JSON)::

    {
      "sample_rate": 48000,
      "channels": 2,
      "block_size": 256,
      "input":  {"type": "synth", "duration_s": 1.0,
                 "freqs_hz": [440, 3000], "seed": 1}
              | {"type": "pcm", "path": "in.pcm", "format": "s16le"},
      "filter":  <filter spec>,              # initial filter, all channels
      "schedule": [{"at_sample": 12000, "filter": <filter spec>,
                    "fade_samples": 512, "channel": null}],
      "output": {"path": "out.f32", "format": "f32le",
                 "report_path": "report.json"}
    }

Schedule events are sample-accurate: the block containing ``at_sample``
is split so the switch happens at exactly that sample.  ``channel`` is
null (all channels) or a channel index.
"""

from __future__ import annotations

import json
import os

import numpy as np

from . import filters, pcm, synth
from .analysis import boundary_jumps
from .core import MultiChannelFIR


def _load_input(spec: dict, job: dict, base_dir: str) -> np.ndarray:
    kind = spec.get("type")
    if kind == "synth":
        return synth.multi_sine(
            num_channels=job["channels"],
            duration_s=spec["duration_s"],
            sample_rate=job["sample_rate"],
            freqs_hz=spec["freqs_hz"],
            seed=spec.get("seed", 0),
            noise_level=spec.get("noise_level", 0.01),
        )
    if kind == "pcm":
        path = os.path.join(base_dir, spec["path"])
        return pcm.read_pcm(path, spec["format"], job["channels"])
    raise ValueError(f"unknown input type: {kind!r}")


def run_job(job: dict, base_dir: str = ".") -> dict:
    """Execute one job dict and return the report (also written to disk
    if ``output.report_path`` is set)."""
    sample_rate = float(job["sample_rate"])
    block_size = int(job["block_size"])
    num_channels = int(job["channels"])
    if block_size < 1:
        raise ValueError("block_size must be >= 1")

    x = _load_input(job["input"], job, base_dir)
    if x.shape[0] != num_channels:
        raise ValueError(
            f"input has {x.shape[0]} channels, job expects {num_channels}"
        )
    total = x.shape[1]

    engine = MultiChannelFIR(num_channels, filters.from_spec(job["filter"], sample_rate))

    events = sorted(job.get("schedule", []), key=lambda e: e["at_sample"])
    for ev in events:
        if not 0 <= int(ev["at_sample"]) < max(total, 1):
            raise ValueError(f"schedule event at_sample={ev['at_sample']} out of range")

    out = np.zeros((num_channels, total), dtype=np.float64)
    applied = []
    pos = 0
    event_idx = 0
    num_blocks = 0
    while pos < total:
        # Split the block at the next scheduled event, if any.
        end = min(pos + block_size, total)
        if event_idx < len(events):
            ev_at = int(events[event_idx]["at_sample"])
            if pos <= ev_at < end:
                end = ev_at if ev_at > pos else pos
        if end == pos:  # event at the current position: apply it first
            ev = events[event_idx]
            engine.switch(
                filters.from_spec(ev["filter"], sample_rate),
                fade_samples=int(ev.get("fade_samples", 0)),
                channel=ev.get("channel"),
            )
            applied.append(
                {
                    "at_sample": int(ev["at_sample"]),
                    "fade_samples": int(ev.get("fade_samples", 0)),
                    "channel": ev.get("channel"),
                    "filter": ev["filter"].get("type", "custom"),
                }
            )
            event_idx += 1
            continue
        out[:, pos:end] = engine.process(x[:, pos:end])
        num_blocks += 1
        pos = end
    # Events scheduled exactly at the end of the signal still apply.
    while event_idx < len(events):
        ev = events[event_idx]
        engine.switch(
            filters.from_spec(ev["filter"], sample_rate),
            fade_samples=int(ev.get("fade_samples", 0)),
            channel=ev.get("channel"),
        )
        applied.append(
            {
                "at_sample": int(ev["at_sample"]),
                "fade_samples": int(ev.get("fade_samples", 0)),
                "channel": ev.get("channel"),
                "filter": ev["filter"].get("type", "custom"),
            }
        )
        event_idx += 1

    output = job["output"]
    out_path = os.path.join(base_dir, output["path"])
    pcm.write_pcm(out_path, out, output["format"])

    # Boundary continuity metric on the emitted block grid.
    max_boundary_jump = max(
        (float(np.max(boundary_jumps(out[ch], block_size))) for ch in range(num_channels)),
        default=0.0,
    )
    report = {
        "sample_rate": sample_rate,
        "channels": num_channels,
        "samples_in": int(total),
        "samples_out": int(out.shape[1]),
        "block_size": block_size,
        "num_blocks": num_blocks,
        "switches_applied": applied,
        "max_abs_output": float(np.max(np.abs(out))) if out.size else 0.0,
        "max_boundary_jump": max_boundary_jump,
        "output_path": out_path,
        "output_format": output["format"],
    }
    report_path = output.get("report_path")
    if report_path:
        with open(os.path.join(base_dir, report_path), "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2, ensure_ascii=False)
    return report
