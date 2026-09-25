#!/usr/bin/env python3
"""独立逐格参考：对小坐标范围的请求用单位网格枚举面积与周长。

仅用于测试交叉验证，与 C++ 扫描线实现完全独立（不同语言、不同算法）。
坐标范围不超过 --grid-limit 时直接逐格枚举；超出时按半开公式用
任意精度整数做区间并集计算（同样独立于被测实现）。
"""
import argparse
import json
import sys


def grid_oracle(rects):
    """单位格枚举：面积=覆盖格数，周长=覆盖格对未覆盖邻格暴露的边数。"""
    nondeg = [r for r in rects if r[0] < r[2] and r[1] < r[3]]
    if not nondeg:
        return 0, 0
    min_x = min(r[0] for r in nondeg)
    max_x = max(r[2] for r in nondeg)
    min_y = min(r[1] for r in nondeg)
    max_y = max(r[3] for r in nondeg)
    w, h = max_x - min_x, max_y - min_y
    covered = set()
    for x1, y1, x2, y2 in nondeg:
        for x in range(x1, x2):
            for y in range(y1, y2):
                covered.add((x, y))
    area = len(covered)
    perimeter = 0
    for x, y in covered:
        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            if (x + dx, y + dy) not in covered:
                perimeter += 1
    return area, perimeter


def interval_union_len(intervals):
    """一维半开区间并集长度（任意精度）。"""
    if not intervals:
        return 0
    intervals = sorted(intervals)
    total = 0
    cur_lo, cur_hi = intervals[0]
    for lo, hi in intervals[1:]:
        if lo > cur_hi:
            total += cur_hi - cur_lo
            cur_lo, cur_hi = lo, hi
        elif hi > cur_hi:
            cur_hi = hi
    total += cur_hi - cur_lo
    return total


def big_oracle(rects):
    """事件扫描 + 一维区间并集，面积与周长分别独立计算（任意精度整数）。"""
    nondeg = [tuple(r) for r in rects if r[0] < r[2] and r[1] < r[3]]
    if not nondeg:
        return 0, 0

    # 面积：x 板条 × y 区间并集长度
    xs = sorted({x for r in nondeg for x in (r[0], r[2])})
    area = 0
    # runs 同时给出：板条内 y 覆盖连续段数 -> 水平边长 = 2 * runs * dx
    perimeter = 0
    for i in range(len(xs) - 1):
        x0, x1 = xs[i], xs[i + 1]
        ivs = sorted((r[1], r[3]) for r in nondeg if r[0] <= x0 and r[2] >= x1)
        # 合并区间
        merged = []
        for lo, hi in ivs:
            if merged and lo <= merged[-1][1]:
                if hi > merged[-1][1]:
                    merged[-1] = (merged[-1][0], hi)
            else:
                merged.append((lo, hi))
        cover = sum(b - a for a, b in merged)
        area += cover * (x1 - x0)
        perimeter += 2 * len(merged) * (x1 - x0)

    # 竖边：每个事件 x 上，按“先开后关”顺序用一维覆盖多重集合模拟，
    # 累计覆盖长度的绝对变化。
    events = []
    for x1, y1, x2, y2 in nondeg:
        events.append((x1, 0, y1, y2))  # 开事件排前
        events.append((x2, 1, y1, y2))  # 关事件排后
    events.sort()
    active = {}  # (lo,hi) -> 计数
    cover_len = 0
    i = 0
    while i < len(events):
        x = events[i][0]
        while i < len(events) and events[i][0] == x:
            _, kind, lo, hi = events[i]
            iv = (lo, hi)
            before = interval_union_len(list(active.keys()))
            if kind == 0:
                active[iv] = active.get(iv, 0) + 1
            else:
                active[iv] -= 1
                if active[iv] == 0:
                    del active[iv]
            after = interval_union_len(list(active.keys()))
            perimeter += abs(after - before)
            i += 1
    return area, perimeter


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("request")
    ap.add_argument("--grid-limit", type=int, default=64)
    args = ap.parse_args()

    with open(args.request, encoding="utf-8") as f:
        req = json.load(f)
    rects = [[int(r[k]) for k in ("x1", "y1", "x2", "y2")]
             for r in req["rectangles"]]

    nondeg = [r for r in rects if r[0] < r[2] and r[1] < r[3]]
    if nondeg:
        span = max(max(r[2], r[3]) - min(r[0], r[1]) for r in nondeg)
    else:
        span = 0
    if span <= args.grid_limit:
        area, perimeter = grid_oracle(rects)
        method = "grid"
    else:
        area, perimeter = big_oracle(rects)
        method = "big"
    print(json.dumps({"area": str(area), "perimeter": str(perimeter),
                      "method": method}))


if __name__ == "__main__":
    sys.exit(main())
