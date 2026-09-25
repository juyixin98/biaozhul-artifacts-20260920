#!/usr/bin/env python3
"""End-to-end CLI/JSON tests for the raybox backend.

Runs the compiled binary as a subprocess, feeds JSON requests on stdin or
via -f, parses the JSON response and asserts numeric/geometric results.
Also cross-checks BVH vs brute-force responses for identical requests.

Usage: python3 tests/test_cli.py ./build/raybox
"""
import json
import math
import os
import random
import subprocess
import sys

FAILURES = []
CHECKS = 0


def check(cond, msg):
    global CHECKS
    CHECKS += 1
    if not cond:
        FAILURES.append(msg)
        print(f"  FAIL: {msg}")


def call(binary, payload, use_file=False):
    if isinstance(payload, str):
        text = payload
    else:
        text = json.dumps(payload)
    if use_file:
        path = os.path.join(os.path.dirname(__file__), "_tmp_req.json")
        with open(path, "w") as f:
            f.write(text)
        p = subprocess.run([binary, "-f", path], capture_output=True, text=True)
        os.remove(path)
    else:
        p = subprocess.run([binary], input=text, capture_output=True, text=True)
    return p.returncode, p.stdout, p.stderr


def req(mode="nearest", use_bvh=True, origin=(0, 0, 0), d=(1, 0, 0),
        boxes=None):
    return {
        "ray": {"origin": list(origin), "dir": list(d)},
        "boxes": boxes if boxes is not None else [],
        "mode": mode,
        "use_bvh": use_bvh,
    }


def B(i, lo, hi):
    return {"id": i, "min": list(lo), "max": list(hi)}


def main():
    binary = sys.argv[1] if len(sys.argv) > 1 else "./build/raybox"
    if not os.path.exists(binary):
        print(f"binary not found: {binary}", file=sys.stderr)
        return 2

    print("[cli] nearest basic hit")
    rc, out, err = call(binary, req(
        origin=(-2, 1, 1), d=(1, 0, 0),
        boxes=[B(0, (0, 0, 0), (2, 2, 2))]))
    r = json.loads(out)
    check(rc == 0 and r["ok"] is True, "ok response")
    check(r["hit"] is True, "hit true")
    check(r["nearest"]["id"] == 0, "nearest id 0")
    check(math.isclose(r["nearest"]["t"], 2.0, abs_tol=1e-12), "t == 2")
    check(r["nearest"]["normal"] == [-1.0, 0.0, 0.0], "entry normal -x")
    check(r["nearest"]["point"] == [0.0, 1.0, 1.0], "entry point")
    check("elapsed_us" in r, "timing present")

    print("[cli] negative-direction normal sign")
    rc, out, _ = call(binary, req(
        origin=(4, 1, 1), d=(-1, 0, 0),
        boxes=[B(7, (0, 0, 0), (2, 2, 2))]))
    r = json.loads(out)
    check(r["nearest"]["normal"] == [1.0, 0.0, 0.0], "normal +x via high face")
    check(math.isclose(r["nearest"]["t"], 2.0), "t == 2 (neg dir)")

    print("[cli] origin inside box")
    rc, out, _ = call(binary, req(
        origin=(1, 1, 1), d=(1, 0, 0),
        boxes=[B(3, (0, 0, 0), (2, 2, 2))]))
    r = json.loads(out)
    check(r["hit"] and r["nearest"]["t"] == 0.0, "inside t == 0")
    check(r["nearest"]["normal"] == [0.0, 0.0, 0.0], "inside zero normal")

    print("[cli] grazing edge")
    rc, out, _ = call(binary, req(
        origin=(-1, 0, 0), d=(1, 0, 0),
        boxes=[B(0, (0, 0, 0), (2, 2, 2))]))
    r = json.loads(out)
    check(r["hit"] and math.isclose(r["nearest"]["t"], 1.0),
          "edge graze hit t == 1")

    print("[cli] degenerate point box")
    rc, out, _ = call(binary, req(
        origin=(-1, -1, -1), d=(1, 1, 1),
        boxes=[B(5, (1, 1, 1), (1, 1, 1))]))
    r = json.loads(out)
    check(r["hit"] and math.isclose(r["nearest"]["t"], 2.0),
          "point box hit at t == 2")

    print("[cli] all-hits sorted ascending")
    boxes = [B(0, (10, -1, -1), (11, 1, 1)),
             B(1, (2, -1, -1), (3, 1, 1)),
             B(2, (5, -1, -1), (6, 1, 1))]
    rc, out, _ = call(binary, req(mode="all", boxes=boxes))
    r = json.loads(out)
    ts = [h["t"] for h in r["hits"]]
    check(r["count"] == 3 and ts == sorted(ts), "3 hits, sorted")
    check([h["id"] for h in r["hits"]] == [1, 2, 0], "order near->far")
    check(all(math.isfinite(h["t"]) for h in r["hits"]), "all t finite")

    print("[cli] BVH vs brute force identical (all modes, random scenes)")
    rng = random.Random(4242)
    mismatches = 0
    for _ in range(300):
        n = rng.randint(1, 25)
        boxes = []
        for i in range(n):
            lo = [rng.uniform(-5, 5) for _ in range(3)]
            hi = [lo[a] + abs(rng.uniform(0, 1.5)) for a in range(3)]
            if rng.random() < 0.1:  # occasional degenerate axis
                ax = rng.randrange(3)
                hi[ax] = lo[ax]
            boxes.append(B(i, lo, hi))
        origin = tuple(rng.uniform(-7, 7) for _ in range(3))
        d = tuple(0.0 if rng.random() < 0.12 else rng.uniform(-1, 1)
                  for _ in range(3))
        if all(v == 0.0 for v in d):
            d = (1.0, 0.0, 0.0)
        for mode in ("nearest", "all"):
            _, o1, _ = call(binary, req(mode, True, origin, d, boxes))
            _, o2, _ = call(binary, req(mode, False, origin, d, boxes))
            a, b = json.loads(o1), json.loads(o2)
            if mode == "nearest":
                ha = a.get("nearest")
                hb = b.get("nearest")
                if a["hit"] != b["hit"]:
                    mismatches += 1
                elif ha and (ha["id"] != hb["id"] or
                             not math.isclose(ha["t"], hb["t"], abs_tol=1e-10)):
                    mismatches += 1
            else:
                if [h["id"] for h in a["hits"]] != [h["id"] for h in b["hits"]]:
                    mismatches += 1
                else:
                    for x, y in zip(a["hits"], b["hits"]):
                        if not math.isclose(x["t"], y["t"], abs_tol=1e-10):
                            mismatches += 1
                            break
    check(mismatches == 0, f"BVH/brute identical over 300 scenes x2 modes "
                           f"(mismatches={mismatches})")

    print("[cli] error: malformed JSON")
    rc, out, _ = call(binary, "{not json")
    r = json.loads(out)
    check(r["ok"] is False and r["error"]["code"] == "invalid_json",
          "invalid_json reported")

    print("[cli] error: zero direction")
    rc, out, _ = call(binary, req(d=(0, 0, 0), boxes=[]))
    r = json.loads(out)
    check(r["ok"] is False and "non-zero" in r["error"]["message"],
          "zero dir rejected")

    print("[cli] error: inverted box")
    rc, out, _ = call(binary, req(
        boxes=[B(0, (1, 0, 0), (0, 2, 2))]))
    r = json.loads(out)
    check(r["ok"] is False and r["error"]["code"] == "degenerate_geometry",
          "inverted box rejected")

    print("[cli] error: NaN literal rejected by parser")
    rc, out, _ = call(binary, '{"ray":{"origin":[NaN,0,0],"dir":[1,0,0]},'
                              '"boxes":[]}')
    r = json.loads(out)
    check(r["ok"] is False and r["error"]["code"] == "invalid_json",
          "NaN literal not valid JSON here")

    print("[cli] empty box set")
    rc, out, _ = call(binary, req(mode="all", boxes=[]))
    r = json.loads(out)
    check(r["ok"] and r["count"] == 0 and r["hits"] == [], "empty all result")
    rc, out, _ = call(binary, req(boxes=[]))
    r = json.loads(out)
    check(r["ok"] and r["hit"] is False, "empty nearest miss")

    print("[cli] file input mode (-f)")
    path = os.path.join(os.path.dirname(__file__), "..", "examples",
                        "nearest.json")
    p = subprocess.run([binary, "-f", path], capture_output=True, text=True)
    r = json.loads(p.stdout)
    check(p.returncode == 0 and r["ok"] and r["nearest"]["id"] == 0,
          "example nearest.json via -f")

    print(f"\n{CHECKS} checks, {len(FAILURES)} failures")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
