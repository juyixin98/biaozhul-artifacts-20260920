#include "geometry.hpp"

#include <algorithm>
#include <initializer_list>

namespace segint {

using i128 = __int128_t;

namespace {

i128 cross(i128 ux, i128 uy, i128 vx, i128 vy) {
    return ux * vy - uy * vx;
}

// 定向值 cross(q-p, r-p)。
i128 orient(const Point& p, const Point& q, const Point& r) {
    return cross(i128(q.x) - p.x, i128(q.y) - p.y,
                 i128(r.x) - p.x, i128(r.y) - p.y);
}

// 闭区间 [l,r]（与 l/r 顺序无关）内是否包含 v。
bool between1d(int64_t l, int64_t v, int64_t r) {
    return std::min(l, r) <= v && v <= std::max(l, r);
}

// 点 p 是否落在非退化线段 uv 的包围盒内（调用前已保证共线）。
bool in_box(const Point& p, const Point& u, const Point& v) {
    return between1d(u.x, p.x, v.x) && between1d(u.y, p.y, v.y);
}

bool opposite_strict(i128 s, i128 t) {
    return (s > 0 && t < 0) || (s < 0 && t > 0);
}

// 两个叉积是否构成“跨立”（允许落在直线上，值为 0）。
// 不使用乘积判断：乘积可能达到 2^252，超出 __int128 范围。
bool straddles(i128 s, i128 t) {
    return s == 0 || t == 0 || opposite_strict(s, t);
}

ContactPoint make_point(int64_t x, int64_t y,
                        std::initializer_list<EndId> hit_ids) {
    ContactPoint cp;
    cp.x = Rational(x);
    cp.y = Rational(y);
    for (EndId e : hit_ids) cp.hits.push_back(end_name(e));
    return cp;
}

}  // namespace

const char* classify_name(Classify c) {
    switch (c) {
        case Classify::None:    return "none";
        case Classify::Cross:   return "cross";
        case Classify::Touch:   return "touch";
        case Classify::Overlap: return "overlap";
    }
    return "none";
}

bool coord_in_range(int64_t v) { return v >= -MAX_COORD && v <= MAX_COORD; }

Result intersect(const Point& a, const Point& b,
                 const Point& c, const Point& d) {
    Result res;

    const bool ab_degen = (a.x == b.x && a.y == b.y);
    const bool cd_degen = (c.x == d.x && c.y == d.y);

    // ---- 退化情形：零长线段视为一个点 ----
    // hits 语义：坐标等于接触点的“全部”端点（零长线段的 a,b 恒同时列出）。
    if (ab_degen && cd_degen) {
        if (a.x == c.x && a.y == c.y) {
            res.cls = Classify::Touch;
            res.points.push_back(
                make_point(a.x, a.y, {EndId::A, EndId::B, EndId::C, EndId::D}));
        }
        return res;
    }
    if (ab_degen) {
        if (orient(c, d, a) == 0 && in_box(a, c, d)) {
            res.cls = Classify::Touch;
            if (a.x == c.x && a.y == c.y) {
                res.points.push_back(
                    make_point(a.x, a.y, {EndId::A, EndId::B, EndId::C}));
            } else if (a.x == d.x && a.y == d.y) {
                res.points.push_back(
                    make_point(a.x, a.y, {EndId::A, EndId::B, EndId::D}));
            } else {
                res.points.push_back(
                    make_point(a.x, a.y, {EndId::A, EndId::B}));
            }
        }
        return res;
    }
    if (cd_degen) {
        if (orient(a, b, c) == 0 && in_box(c, a, b)) {
            res.cls = Classify::Touch;
            if (c.x == a.x && c.y == a.y) {
                res.points.push_back(
                    make_point(c.x, c.y, {EndId::A, EndId::C, EndId::D}));
            } else if (c.x == b.x && c.y == b.y) {
                res.points.push_back(
                    make_point(c.x, c.y, {EndId::B, EndId::C, EndId::D}));
            } else {
                res.points.push_back(
                    make_point(c.x, c.y, {EndId::C, EndId::D}));
            }
        }
        return res;
    }

    // ---- 两条非退化线段 ----
    const i128 abx = i128(b.x) - a.x;
    const i128 aby = i128(b.y) - a.y;
    const i128 cdx = i128(d.x) - c.x;
    const i128 cdy = i128(d.y) - c.y;

    const i128 den = cross(abx, aby, cdx, cdy);
    const i128 oab_c = orient(a, b, c);
    const i128 oab_d = orient(a, b, d);
    const i128 ocd_a = orient(c, d, a);
    const i128 ocd_b = orient(c, d, b);

    if (den != 0) {
        // 所在直线相交于唯一点；判断该点是否同时落在两条线段上。
        if (!straddles(oab_c, oab_d) || !straddles(ocd_a, ocd_b)) {
            return res;  // none
        }

        if (oab_c != 0 && oab_d != 0 && ocd_a != 0 && ocd_b != 0) {
            // 四个端点均严格位于对方直线两侧 => 交点在两条线段内部 => cross。
            res.cls = Classify::Cross;
        } else {
            // 交点恰为某条线段的端点（端点接触）。
            res.cls = Classify::Touch;
            std::vector<EndId> hits;
            int64_t px = 0, py = 0;
            // 非退化保证 a≠b、c≠d，故每组至多一个端点落在对方直线上；
            // 但可能同时 a==c，即两条线段的端点重合于同一点。
            if (ocd_a == 0) { hits.push_back(EndId::A); px = a.x; py = a.y; }
            if (ocd_b == 0) { hits.push_back(EndId::B); px = b.x; py = b.y; }
            if (oab_c == 0) { hits.push_back(EndId::C); px = c.x; py = c.y; }
            if (oab_d == 0) { hits.push_back(EndId::D); px = d.x; py = d.y; }
            res.points.push_back(make_point(px, py, {hits[0]}));
            if (hits.size() > 1) res.points[0].hits.push_back(end_name(hits[1]));
            return res;
        }

        // 用任意精度整数计算交点参数 t = cross(c-a, d-c) / den，
        // 再由 p = a + t*(b-a) 得到分数坐标。
        cpp_int AX(a.x), AY(a.y), BX(b.x), BY(b.y), CX(c.x), CY(c.y), DX(d.x), DY(d.y);
        cpp_int denc = (BX - AX) * (DY - CY) - (BY - AY) * (DX - CX);
        cpp_int tnum = (CX - AX) * (DY - CY) - (CY - AY) * (DX - CX);
        ContactPoint cp;
        cp.x = Rational(AX * denc + tnum * (BX - AX), denc);
        cp.y = Rational(AY * denc + tnum * (BY - AY), denc);
        res.points.push_back(std::move(cp));
        return res;
    }

    // den == 0：两线段方向平行。
    if (oab_c != 0) return res;  // 不共线 => none

    // 共线：在一个非退化的投影轴上做一维区间相交。
    const bool use_x = (abx != 0);
    auto proj = [&](const Point& p) -> int64_t { return use_x ? p.x : p.y; };
    const int64_t pa = proj(a), pb = proj(b);
    const int64_t pc = proj(c), pd = proj(d);
    const int64_t lo1 = std::min(pa, pb), hi1 = std::max(pa, pb);
    const int64_t lo2 = std::min(pc, pd), hi2 = std::max(pc, pd);
    const int64_t lo = std::max(lo1, lo2);
    const int64_t hi = std::min(hi1, hi2);

    if (lo > hi) return res;  // 共线但分离

    // 收集投影值等于给定值的端点（共线 + 非退化 => 投影相同即同一点）。
    auto endpoints_at = [&](int64_t v) {
        std::vector<EndId> es;
        if (proj(a) == v) es.push_back(EndId::A);
        if (proj(b) == v) es.push_back(EndId::B);
        if (proj(c) == v) es.push_back(EndId::C);
        if (proj(d) == v) es.push_back(EndId::D);
        return es;
    };
    auto point_of = [&](EndId e) -> Point {
        switch (e) {
            case EndId::A: return a;
            case EndId::B: return b;
            case EndId::C: return c;
            case EndId::D: return d;
        }
        return a;
    };
    auto to_contact = [&](const std::vector<EndId>& es) {
        const Point p = point_of(es[0]);
        ContactPoint cp;
        cp.x = Rational(p.x);
        cp.y = Rational(p.y);
        for (EndId e : es) cp.hits.push_back(end_name(e));
        return cp;
    };

    if (lo == hi) {
        res.cls = Classify::Touch;  // 共线但仅端点相触
        res.points.push_back(to_contact(endpoints_at(lo)));
        return res;
    }

    res.cls = Classify::Overlap;  // 共享长度为正的区间
    res.points.push_back(to_contact(endpoints_at(lo)));
    res.points.push_back(to_contact(endpoints_at(hi)));
    return res;
}

}  // namespace segint
