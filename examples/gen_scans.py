#!/usr/bin/env python3
"""Dev-only generator for examples/scans.json (the generated JSON is the
shipped example; the service itself has no Python dependency).

Scene: a square 5 m x 5 m room centred on the origin, walls at x/y = +/-2.5,
with an open doorway on the +x wall (beams through it return nothing).
"""
import json
import math

HALF = 2.5
MAX_RANGE = 10.0


def room_range(a):
    """Range to the first axis-aligned wall on bearing a."""
    tx = HALF / abs(math.cos(a)) if abs(math.cos(a)) > 1e-12 else 1e9
    ty = HALF / abs(math.sin(a)) if abs(math.sin(a)) > 1e-12 else 1e9
    return min(tx, ty)


def scan(px, py, theta, step_deg):
    beams = []
    n = int(round(360.0 / step_deg))
    for k in range(n):
        a = math.radians(k * step_deg)  # beam angle in body frame
        wa = theta + a                  # world bearing
        # Doorway: world +x direction, +/-12 degrees -> no return.
        d = math.atan2(math.sin(wa), math.cos(wa))
        if abs(d) < math.radians(12.0):
            beams.append({"angle": round(a, 9), "range": 0.0,
                          "no_return": True})
        else:
            r = room_range(wa)
            beams.append({"angle": round(a, 9),
                          "range": round(r, 6)})
    return {"pose": {"x": px, "y": py, "theta": theta},
            "max_range": MAX_RANGE, "beams": beams}


scans = [
    scan(0.0, 0.0, 0.0, 5.0),          # full 360 sweep at room centre
    scan(-1.0, -1.0, 0.35, 5.0),       # offset, rotated viewpoint
]
# Repeated central sweeps: repeated observations reinforce walls until the
# log-odds hit the configured saturation bound.
for _ in range(4):
    scans.append(scan(0.0, 0.0, 0.0, 5.0))

with open("examples/scans.json", "w") as f:
    json.dump({"scans": scans}, f, indent=1)
print("wrote examples/scans.json:",
      sum(len(s["beams"]) for s in scans), "beams in", len(scans), "scans")
