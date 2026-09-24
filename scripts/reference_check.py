#!/usr/bin/env python3
"""Cross-language beam-by-beam reference checker.

Independently reimplements the fusion rules in Python:
  * pose+returns -> world beams (range < 0 / >= max_range => no-return),
  * slab clipping + Amanatides-Woo grid traversal,
  * open-segment cells free, endpoint cell occupied, no-return free-only,
  * clamped log-odds, separate observed mask,
  * GRID-STATE:v1 state digest and the version-chain digest.

It drives the running service over HTTP (using the same signing scheme as
pf_client.py), applies the example scans, exports every version, and asserts
the service output agrees cell-by-cell with this reference and that the
Python-recomputed digests match the server-reported digests.

Usage:
    PF_SECRET=... ./reference_check.py --base http://127.0.0.1:8080 \
        --examples examples/
"""

import argparse
import hashlib
import json
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pf_client  # noqa: E402

INF = float("inf")
EPS = 1e-9


def beam_from_return(pose, angle, rng, max_range):
    x, y, theta = pose
    a = theta + angle
    if rng >= 0 and rng < max_range:
        return (x, y, x + rng * math.cos(a), y + rng * math.sin(a),
                True, max_range)
    return (x, y, x + max_range * math.cos(a),
            y + max_range * math.sin(a), False, max_range)


def cast_beam(cfg, b):
    """Return (free:set[(ix,iy)], occupied:set[(ix,iy)])."""
    ox, oy, ex, ey, hit, max_range = b
    if not hit:
        dx, dy = ex - ox, ey - oy
        ln = math.hypot(dx, dy)
        ex = ox + dx / ln * max_range
        ey = oy + dy / ln * max_range
    dx, dy = ex - ox, ey - oy
    ray_len = math.hypot(dx, dy)
    if ray_len <= 0.0:
        if hit:
            ix = math.floor((ox - cfg["origin"][0]) / cfg["resolution"])
            iy = math.floor((oy - cfg["origin"][1]) / cfg["resolution"])
            if 0 <= ix < cfg["width"] and 0 <= iy < cfg["height"]:
                return set(), {(ix, iy)}
        return set(), set()

    ux, uy = dx / ray_len, dy / ray_len
    res = cfg["resolution"]
    x0, y0 = cfg["origin"]
    x1, y1 = x0 + cfg["width"] * res, y0 + cfg["height"] * res

    # Slab intersection, t in [0,1].
    t_near, t_far = 0.0, 1.0

    def clip(o, d, lo, hi):
        nonlocal t_near, t_far
        if abs(d) < 1e-15:
            return lo <= o <= hi
        ta, tb = (lo - o) / d, (hi - o) / d
        if ta > tb:
            ta, tb = tb, ta
        t_near, t_far = max(t_near, ta), min(t_far, tb)
        return t_near <= t_far + 1e-12

    if not (clip(ox, dx, x0, x1) and clip(oy, dy, y0, y1)):
        return set(), set()
    if t_far < 0 or t_near > 1:
        return set(), set()
    t_near, t_far = min(max(t_near, 0.0), 1.0), min(max(t_far, 0.0), 1.0)

    end_inside = (hit and x0 < ex < x1 and y0 < ey < y1)
    end_cell = None
    if end_inside:
        eix = math.floor((ex - x0) / res)
        eiy = math.floor((ey - y0) / res)
        if 0 <= eix < cfg["width"] and 0 <= eiy < cfg["height"]:
            end_cell = (eix, eiy)
        else:
            end_inside = False

    px = ox + dx * t_near + ux * res * 1e-12
    py = oy + dy * t_near + uy * res * 1e-12
    ix = min(max(math.floor((px - x0) / res), 0), cfg["width"] - 1)
    iy = min(max(math.floor((py - y0) / res), 0), cfg["height"] - 1)
    sx = 1 if ux > 1e-15 else (-1 if ux < -1e-15 else 0)
    sy = 1 if uy > 1e-15 else (-1 if uy < -1e-15 else 0)

    tmx = INF if sx == 0 else (
        (x0 + (ix + 1) * res if sx > 0 else x0 + ix * res) - px) / ux
    tmy = INF if sy == 0 else (
        (y0 + (iy + 1) * res if sy > 0 else y0 + iy * res) - py) / uy
    tdx = INF if sx == 0 else res / abs(ux)
    tdy = INF if sy == 0 else res / abs(uy)
    dist_total = (t_far - t_near) * ray_len
    eps = EPS * res

    free = set()
    for _ in range(cfg["width"] + cfg["height"] + 4):
        if not (0 <= ix < cfg["width"] and 0 <= iy < cfg["height"]):
            break
        cell = (ix, iy)
        if not (end_inside and cell == end_cell):
            free.add(cell)
        advance = min(tmx, tmy)
        if advance >= dist_total - eps:
            break
        tmx -= advance
        tmy -= advance
        dist_total -= advance
        if tmx <= eps and tmy <= eps:
            ix += sx
            iy += sy
            tmx, tmy = tdx, tdy
        elif tmx <= eps:
            ix += sx
            tmx = tdx
        else:
            iy += sy
            tmy = tdy
    occupied = {end_cell} if end_inside else set()
    return free, occupied


class RefGrid:
    def __init__(self, cfg, p_hit=0.6, p_free=0.4, l_max=3.0):
        self.cfg = cfg
        self.lh = math.log(p_hit / (1 - p_hit))
        self.lf = math.log(p_free / (1 - p_free))
        self.l_max = l_max
        n = cfg["width"] * cfg["height"]
        self.lo = [0.0] * n
        self.obs = [0] * n

    def apply(self, beam):
        free, occ = cast_beam(self.cfg, beam)
        for ix, iy in free:
            k = iy * self.cfg["width"] + ix
            self.lo[k] = min(max(self.lo[k] + self.lf, -self.l_max),
                             self.l_max)
            self.obs[k] = 1
        for ix, iy in occ:
            k = iy * self.cfg["width"] + ix
            self.lo[k] = min(max(self.lo[k] + self.lh, -self.l_max),
                             self.l_max)
            self.obs[k] = 1

    def state_digest(self):
        c = self.cfg
        lines = ["GRID-STATE:v1",
                 f"resolution={repr(c['resolution'])}",
                 f"origin_x={repr(c['origin'][0])}",
                 f"origin_y={repr(c['origin'][1])}",
                 f"width={c['width']}", f"height={c['height']}"]
        for k in range(c["width"] * c["height"]):
            iy, ix = divmod(k, c["width"])
            micro = math.floor(self.lo[k] * 1e6 + 0.5)
            lines.append(f"{ix},{iy},{micro},{self.obs[k]}")
        return hashlib.sha256(
            ("\n".join(lines) + "\n").encode()).hexdigest()


def chain_digest(seq, parent, payload, state):
    s = ("GRID-VERSION:v1\n"
         f"seq={seq}\nparent={parent}\npayload={payload}\nstate={state}\n")
    return hashlib.sha256(s.encode()).hexdigest()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8080")
    ap.add_argument("--examples", default="examples")
    ap.add_argument("--secret", default=os.environ.get("PF_SECRET"))
    args = ap.parse_args()
    if not args.secret:
        sys.exit("PF_SECRET required")

    ex = args.examples
    create = json.load(open(os.path.join(ex, "create_map.json")))
    status, text = pf_client.request(
        args.base, "POST", "/v1/maps", args.secret,
        open(os.path.join(ex, "create_map.json")).read())
    assert status == 201, (status, text)
    created = json.loads(text)
    map_id = created["id"]

    ref = RefGrid(create, create.get("p_hit", 0.6),
                  create.get("p_free", 0.4), create.get("l_max", 3.0))

    # v0 cross-check.
    status, text = pf_client.request(
        args.base, "GET", f"/v1/maps/{map_id}/grid?seq=0", args.secret)
    assert status == 200, text
    assert json.loads(text)["state_digest"] == ref.state_digest(), \
        "v0 state digest mismatch"

    scan_files = ["scan_wall.json", "scan_no_return.json",
                  "scan_raw_beams.json"]

    for name in scan_files:
        path = os.path.join(ex, name)
        if not os.path.exists(path):
            continue
        scan = json.load(open(path))
        # Build beams exactly like the protocol layer.
        beams = []
        if "returns" in scan:
            mr = scan["max_range"]
            for r in scan["returns"]:
                if isinstance(r, list):
                    a, rng = r
                else:
                    a, rng = r["angle"], r["range"]
                beams.append(beam_from_return(scan["pose"], a, rng, mr))
        else:
            for bj in scan["beams"]:
                beams.append(tuple(bj))
        for b in beams:
            ref.apply(b)

        status, text = pf_client.request(
            args.base, "POST", f"/v1/maps/{map_id}/scans", args.secret,
            json.dumps(scan))
        assert status == 200, (name, status, text)
        svc = json.loads(text)

        status, text = pf_client.request(
            args.base, "GET", f"/v1/maps/{map_id}/grid",
            args.secret)
        exported = json.loads(text)

        # 1) cell-by-cell numeric comparison
        rows = exported["grid"]["cells"]
        mismatches = 0
        for iy, row in enumerate(rows):
            for ix, cell in enumerate(row):
                k = iy * create["width"] + ix
                if cell["state"] == "unknown":
                    if ref.obs[k] or abs(ref.lo[k]) > 1e-12:
                        mismatches += 1
                else:
                    if not ref.obs[k]:
                        mismatches += 1
                        continue
                    if abs(cell["log_odds"] - ref.lo[k]) > 1e-9:
                        mismatches += 1
        assert mismatches == 0, f"{name}: {mismatches} cells differ"

        # 2) Python-recomputed state digest == server state digest
        py_state = ref.state_digest()
        assert py_state == exported["state_digest"] == \
            svc["version"]["state_digest"], (
            f"{name}: state digest mismatch")

    # 3) whole version chain recomputed in Python from reported payloads
    status, text = pf_client.request(
        args.base, "GET", f"/v1/maps/{map_id}/versions", args.secret)
    versions = json.loads(text)["versions"]
    parent = "0" * 64
    for v in versions:
        expect = chain_digest(v["seq"], parent, v["payload_digest"],
                              v["state_digest"])
        assert expect == v["version_digest"], \
            f"chain mismatch at seq {v['seq']}"
        parent = v["version_digest"]

    # 4) server-side integrity endpoint agrees
    status, text = pf_client.request(
        args.base, "GET", f"/v1/maps/{map_id}/verify", args.secret)
    assert status == 200 and json.loads(text)["ok"]

    print(f"reference_check OK: {len(versions)} versions, "
          f"final state {versions[-1]['version_digest'][:16]}..., map "
          f"{map_id}")


if __name__ == "__main__":
    main()
