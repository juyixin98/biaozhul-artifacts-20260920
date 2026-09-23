#!/usr/bin/env python3
"""End-to-end tests for the JSON request entry point: success shapes,
decimal scaling, validation-only mode, and every documented error path.
"""
import json
import os
import subprocess
import sys

BINARY = sys.argv[1] if len(sys.argv) > 1 else "./pointloc"
failures = []


def run(payload):
    proc = subprocess.run([BINARY], input=json.dumps(payload),
                          capture_output=True, text=True)
    return proc.returncode, json.loads(proc.stdout)


def expect_ok(cond, msg):
    if not cond:
        failures.append(msg)


# 1. Basic batch with a hole; default engine is indexed.
rc, out = run({
    "polygon": {
        "outer": [{"x": 0, "y": 0}, {"x": 20, "y": 0},
                  {"x": 20, "y": 20}, {"x": 0, "y": 20}],
        "holes": [[{"x": 5, "y": 5}, {"x": 10, "y": 5},
                  {"x": 10, "y": 10}, {"x": 5, "y": 10}]],
    },
    "queries": [{"x": 7, "y": 7}, {"x": 2, "y": 2},
                {"x": 7, "y": 5}, {"x": -1, "y": 0}],
})
expect_ok(rc == 0 and out["status"] == "ok", f"basic ok: {out}")
expect_ok(out["method"] == "indexed", f"default method: {out.get('method')}")
locs = [r["location"] for r in out["results"]]
expect_ok(locs == ["outside", "inside", "boundary", "outside"],
          f"basic classifications {locs}")
expect_ok(out["meta"]["scale"] == 0, "integer scale should be 0")
expect_ok(out["stats"]["bands"] >= 1, "bands reported")

# 2. Mixed decimal precision aligned to a common scale exactly.
rc, out = run({
    "polygon": {"outer": [{"x": 0, "y": 0}, {"x": 1, "y": 0},
                          {"x": 1, "y": 1}, {"x": 0, "y": 1}]},
    "queries": [{"x": 0.5, "y": 0.25}, {"x": 0.001, "y": 0.001}],
})
expect_ok(rc == 0, f"decimal rc={rc} {out}")
expect_ok(out["meta"]["scale"] == 3, f"decimal scale {out.get('meta')}")
locs = [r["location"] for r in out["results"]]
expect_ok(locs == ["inside", "inside"], f"decimal locs {locs}")

# 3. Naive engine explicitly.
rc, out = run({
    "method": "naive",
    "polygon": {"outer": [{"x": 0, "y": 0}, {"x": 2, "y": 0},
                          {"x": 2, "y": 2}, {"x": 0, "y": 2}]},
    "queries": [{"x": 1, "y": 1}],
})
expect_ok(rc == 0 and out["method"] == "naive", f"naive: {out}")

# 4. Validation only (no queries).
rc, out = run({
    "polygon": {"outer": [{"x": 0, "y": 0}, {"x": 5, "y": 0},
                          {"x": 5, "y": 5}, {"x": 0, "y": 5}]}
})
expect_ok(rc == 0 and out.get("valid") is True, f"validate-only: {out}")

# 5. Coordinates are echoed verbatim (no float reformatting).
rc, out = run({
    "polygon": {"outer": [{"x": 0, "y": 0}, {"x": 10, "y": 0},
                          {"x": 10, "y": 10}, {"x": 0, "y": 10}]},
    "queries": [{"x": 1.50, "y": 2.25}],
})
expect_ok(rc == 0, f"echo rc {out}")
expect_ok(out["results"][0]["query"]["x"] == 1.50
          and str(out["results"][0]["query"]["y"]) == "2.25",
          f"echo values {out['results'][0]['query']}")

# ---- error paths ----------------------------------------------------------
def expect_error(payload, code, rc_expected):
    rc, out = run(payload)
    expect_ok(rc == rc_expected, f"{code}: rc={rc}")
    expect_ok(out.get("error") == code or out.get("status") == "error"
              and code in json.dumps(out),
              f"{code}: unexpected {out}")


expect_error({"polygon": {"outer": [{"x": 0, "y": 0}, {"x": 1, "y": 1}]}},
             "invalid_request", 2)  # too few points -> request shape error
expect_error({"polygon": {"outer": [{"x": 0, "y": 0}, {"x": 5, "y": 0},
                                    {"x": 5, "y": 5}, {"x": 0, "y": 5}],
                          "holes": [[{"x": 0, "y": 1}, {"x": 5, "y": 1},
                                     {"x": 5, "y": 4}, {"x": 0, "y": 4}]]}},
             "hole_outside_outer", 1)  # hole touching outer
expect_error({"polygon": {"outer": [{"x": 0, "y": 0}, {"x": 10, "y": 10},
                                    {"x": 10, "y": 0}, {"x": 0, "y": 10}]}},
             "invalid_ring", 1)  # bowtie
expect_error({"polygon": {"outer": [{"x": 0, "y": 0}, {"x": 10, "y": 0},
                                    {"x": 10, "y": 10}, {"x": 0, "y": 10}],
                          "holes": [[{"x": 2, "y": 2}, {"x": 8, "y": 2},
                                     {"x": 8, "y": 8}, {"x": 2, "y": 8}],
                                    [{"x": 4, "y": 4}, {"x": 6, "y": 4},
                                     {"x": 6, "y": 6}, {"x": 4, "y": 6}]]}},
             "holes_intersect_or_nested", 1)

# Malformed JSON -> invalid_json, rc 2.
proc = subprocess.run([BINARY], input="{not json", capture_output=True,
                      text=True)
expect_ok(proc.returncode == 2 and "invalid_json" in proc.stdout,
          f"malformed json: {proc.stdout}")

# Coordinate out of range (>4.6e18).
expect_error({"polygon": {"outer": [{"x": 0, "y": 0},
                                    {"x": 5000000000000000000, "y": 0},
                                    {"x": 5000000000000000000,
                                     "y": 5000000000000000000},
                                    {"x": 0, "y": 5000000000000000000}]}},
             "coordinate_out_of_range", 2)

# Too many decimal places.
expect_error({"polygon": {"outer": [{"x": 0, "y": 0}, {"x": 1, "y": 0},
                                    {"x": 1, "y": 1.0000000000001},
                                    {"x": 0, "y": 1}]}},
             "coordinate_precision_exceeded", 2)

print(f"{len(failures)} failures")
for f in failures:
    print("  FAIL " + f)
sys.exit(1 if failures else 0)
