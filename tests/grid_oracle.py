"""网格穷举对照器：独立于分支定界实现的小规模整数精确基准。

仅用于测试。把 W × H 的条带离散为单位方格，用位图记录每个矩形
可能占据的方格集合，DFS 枚举不重叠放置（矩形坐标取整数）。
对整数尺寸的小实例，最优连续布局必落在整数网格上，因此该枚举
结果即为真实最优高度，可与
:func:`strip_packing.exact.exact_packing` 交叉验证。
"""

from __future__ import annotations


def _place_masks(W, H, w, h):
    """矩形 (w,h) 在 W×H 网格内所有整数放置对应的方格位掩码列表。"""
    out = []
    for x in range(W - w + 1):
        for y in range(H - h + 1):
            m = 0
            for dx in range(w):
                for dy in range(h):
                    m |= 1 << ((y + dy) * W + (x + dx))
            out.append(m)
    return out


def grid_optimal_height(rects, W, max_height):
    """返回能放下全部矩形的最小整数高度；放不下 max_height 时返回 None。"""
    dims = sorted([(int(w), int(h)) for w, h in rects],
                  key=lambda t: (-(t[0] * t[1]), -t[1]))
    for H in range(1, int(max_height) + 1):
        masks = [_place_masks(W, H, w, h) for w, h in dims]
        if all(masks):
            if _dfs(masks, 0, 0):
                return H
    return None


def _dfs(masks, k, occupied):
    if k == len(masks):
        return True
    for m in masks[k]:
        if m & occupied == 0:
            if _dfs(masks, k + 1, occupied | m):
                return True
    return False


def grid_layout(rects, W, H):
    """在给定高度 H 下寻找一份整数网格布局，返回 [(id,x,y,w,h)] 或 None。"""
    order = sorted(enumerate(rects),
                   key=lambda t: (-(t[1][1] * t[1][2]), -t[1][2]))
    chosen = [None] * len(rects)

    def dfs(k, occupied):
        if k == len(order):
            return True
        orig_idx, (rid, rw, rh) = order[k]
        w, h = int(rw), int(rh)
        for x in range(W - w + 1):
            for y in range(H - h + 1):
                m = 0
                for dx in range(w):
                    for dy in range(h):
                        m |= 1 << ((y + dy) * W + (x + dx))
                if m & occupied == 0:
                    chosen[orig_idx] = (rid, x, y, w, h)
                    if dfs(k + 1, occupied | m):
                        return True
                    chosen[orig_idx] = None
        return False

    return chosen if dfs(0, 0) else None
