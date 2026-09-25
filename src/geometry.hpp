// geometry.hpp — 轴对齐矩形并集的面积与周长（扫描线 + 线段树）
//
// 语义约定：
//   每个矩形为半开区域 [x1, x2) × [y1, y2)，坐标为 int64 整数，要求
//   x1 <= x2、y1 <= y2。x1 == x2 或 y1 == y2 时矩形为空集（零面积），
//   对面积与周长贡献均为 0。
//
// 精度：
//   坐标跨度最大可达 2^64-1，面积最大可达约 2^128，故内部统一使用
//   unsigned __int128，结果由调用方序列化为十进制字符串，全程无浮点。
#pragma once

#include <algorithm>
#include <cstdint>
#include <vector>
#include <string>

namespace rectunion {

using u128 = __uint128_t;
using i64 = long long;

struct Rect {
    i64 x1, y1, x2, y2;
};

struct Metrics {
    u128 area;
    u128 perimeter;
};

inline std::string toString(u128 v) {
    if (v == 0) return "0";
    std::string s;
    while (v > 0) {
        s.push_back(char('0' + v % 10));
        v /= 10;
    }
    std::reverse(s.begin(), s.end());
    return s;
}

namespace detail {

// 对 y 离散化后的基本段维护区间覆盖计数。
// 每个节点保存：
//   cov   —— 完整覆盖该节点区间的活动矩形数（区间加标记）
//   len   —— 该区间被覆盖的总长度（cov>0 时为整段长度，否则合并子节点）
//   runs  —— 被覆盖的连续段数量
//   lc/rc —— 区间最左/最右基本段是否被覆盖（用于合并 runs）
class SegTree {
public:
    explicit SegTree(const std::vector<i64>& ys)
        : ys_(ys), n_(static_cast<int>(ys.size()) - 1) {
        int sz = 1;
        while (sz < n_) sz <<= 1;
        size_t cap = static_cast<size_t>(sz) * 2;
        cov_.assign(cap, 0);
        len_.assign(cap, 0);
        runs_.assign(cap, 0);
        lc_.assign(cap, 0);
        rc_.assign(cap, 0);
    }

    // 对基本段下标区间 [lo, hi) 加 delta（+1 / -1）。
    void add(int lo, int hi, int delta) { add(1, 0, n_, lo, hi, delta); }

    u128 coveredLen() const { return len_[1]; }
    int runs() const { return runs_[1]; }

private:
    void add(int node, int l, int r, int ql, int qr, int d) {
        if (ql <= l && r <= qr) {
            cov_[node] += d;
        } else {
            int mid = (l + r) >> 1;
            if (ql < mid) add(node << 1, l, mid, ql, qr, d);
            if (qr > mid) add(node << 1 | 1, mid, r, ql, qr, d);
        }
        pull(node, l, r);
    }

    void pull(int node, int l, int r) {
        if (cov_[node] > 0) {
            len_[node] = static_cast<u128>(ys_[r]) - static_cast<u128>(ys_[l]);
            runs_[node] = 1;
            lc_[node] = rc_[node] = 1;
        } else if (r - l == 1) {
            len_[node] = 0;
            runs_[node] = 0;
            lc_[node] = rc_[node] = 0;
        } else {
            int a = node << 1, b = a | 1;
            len_[node] = len_[a] + len_[b];
            runs_[node] = runs_[a] + runs_[b] - (rc_[a] && lc_[b] ? 1 : 0);
            lc_[node] = lc_[a];
            rc_[node] = rc_[b];
        }
    }

    const std::vector<i64>& ys_;
    int n_;
    std::vector<int> cov_;
    std::vector<u128> len_;
    std::vector<int> runs_;
    std::vector<unsigned char> lc_, rc_;
};

} // namespace detail

// 计算矩形并集的面积与周长。
//
// 扫描线在每个事件 x 处：
//   1. 垂直边界：同一 x 的所有事件按“先加（+1）后减（-1）”顺序作用于线段树，
//      累计每次更新根节点覆盖长度的绝对变化。该值等于事件左、右两侧 y 覆盖
//      集合的对称差长度——即该 x 处真正属于并集边界的竖边长度。相邻矩形在
//      贴合边上一减一加相互抵消（计数 0），故公共边不会重复计数；嵌套矩形
//      的内部边同样抵消。
//   2. 板条 (x, nextX)：面积 += 覆盖长度 × 板条宽；
//      水平边界 = 2 × 覆盖连续段数 × 板条宽（每段的上、下各一条边）。
inline Metrics unionMetrics(const std::vector<Rect>& rects) {
    struct Event {
        i64 x;
        int delta; // +1: 矩形左缘, -1: 右缘
        i64 y1, y2;
    };

    std::vector<Event> events;
    std::vector<i64> ys;
    events.reserve(rects.size() * 2);
    ys.reserve(rects.size() * 2);

    for (const Rect& r : rects) {
        // 调用方已保证 x1<=x2、y1<=y2；相等即为零面积空集，跳过。
        if (r.x1 == r.x2 || r.y1 == r.y2) continue;
        events.push_back({r.x1, +1, r.y1, r.y2});
        events.push_back({r.x2, -1, r.y1, r.y2});
        ys.push_back(r.y1);
        ys.push_back(r.y2);
    }

    if (events.empty()) return {0, 0};

    // 同 x：+1 排在 -1 前（delta 降序），这是竖边不重复计数的关键。
    std::sort(events.begin(), events.end(), [](const Event& a, const Event& b) {
        if (a.x != b.x) return a.x < b.x;
        return a.delta > b.delta;
    });
    std::sort(ys.begin(), ys.end());
    ys.erase(std::unique(ys.begin(), ys.end()), ys.end());

    detail::SegTree tree(ys);

    Metrics m{0, 0};
    size_t i = 0;
    while (i < events.size()) {
        const i64 x = events[i].x;

        // 处理同一 x 的全部事件，累计竖边长度。
        do {
            const Event& ev = events[i];
            int lo = static_cast<int>(
                std::lower_bound(ys.begin(), ys.end(), ev.y1) - ys.begin());
            int hi = static_cast<int>(
                std::lower_bound(ys.begin(), ys.end(), ev.y2) - ys.begin());
            u128 before = tree.coveredLen();
            tree.add(lo, hi, ev.delta);
            u128 after = tree.coveredLen();
            m.perimeter += (after >= before) ? (after - before) : (before - after);
            ++i;
        } while (i < events.size() && events[i].x == x);

        // 右侧板条上的面积与水平边界。
        if (i < events.size()) {
            u128 dx = static_cast<u128>(events[i].x) - static_cast<u128>(x);
            m.area += tree.coveredLen() * dx;
            m.perimeter += 2u * static_cast<u128>(tree.runs()) * dx;
        }
    }

    return m;
}

} // namespace rectunion
