#!/usr/bin/env python3
"""独立交叉校验：用 Python 对每个样例的输入重新做离线完整分组，
与服务返回的 finalResults / stats 逐条比对。与 Java 实现完全独立。"""
import json
import pathlib
import sys

OUT = pathlib.Path("samples/out")
SAMPLES = pathlib.Path("samples")


def group_offline(events, gap, agg):
    by_key = {}
    for e in events:
        by_key.setdefault(e["key"], []).append(e)
    rows = []
    for key, es in by_key.items():
        es.sort(key=lambda e: e["timestamp"])
        start = prev = None
        acc = 0
        count = 0
        for e in es:
            t = e["timestamp"]
            if start is None:
                start = prev = t
                acc = e.get("value", 1)
                count = 1
            elif t - prev <= gap:
                acc += e.get("value", 1)
                count += 1
                prev = t
            else:
                rows.append((key, start, prev, count if agg == "COUNT" else acc))
                start = prev = t
                acc = e.get("value", 1)
                count = 1
        rows.append((key, start, prev, count if agg == "COUNT" else acc))
    return sorted(rows)


def simulate_accepted(req):
    """复刻门控语义，算出被接受的事件（离线参考只对这些事件分组）。"""
    gap = req["gap"]
    late = req.get("allowedLateness", 0)
    mode = req.get("watermarkStrategy", {}).get("type", "BOUNDED")
    moo = req.get("watermarkStrategy", {}).get("maxOutOfOrderness", 0)
    wm = None  # None = -inf
    accepted, received = [], 0
    for item in req["input"]:
        if "watermark" in item:
            w = item["watermark"]
            wm = w if wm is None else max(wm, w)
        else:
            received += 1
            t = item["timestamp"]
            if wm is not None and t < wm - late:
                continue  # dropped
            accepted.append(item)
            if mode == "BOUNDED":
                w = t - moo
                wm = w if wm is None else max(wm, w)
    return accepted, received


failures = 0
for req_file in sorted(SAMPLES.glob("0*.json")):
    req = json.loads(req_file.read_text())
    resp = json.loads((OUT / (req_file.stem + ".response.json")).read_text())
    accepted, received = simulate_accepted(req)
    expected = group_offline(accepted, req["gap"], req.get("aggregate", "COUNT"))
    actual = sorted((r["key"], r["start"], r["end"], r["aggregate"])
                    for r in resp["finalResults"])
    ok = expected == actual
    drops = received - len(accepted)
    stats_ok = resp["stats"]["receivedEvents"] == received and \
        resp["stats"]["droppedLateEvents"] == drops
    print(f"{req_file.name}: windows {'OK' if ok else 'MISMATCH'}; "
          f"received={received} drops={drops} stats {'OK' if stats_ok else 'MISMATCH'}")
    if not ok:
        print("  expected:", expected)
        print("  actual:  ", actual)
    if not stats_ok:
        print("  stats:", resp["stats"])
    failures += not (ok and stats_ok)

sys.exit(1 if failures else 0)
