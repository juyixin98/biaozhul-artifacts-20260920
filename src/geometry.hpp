// geometry.hpp - 2D 平面几何：简单多边形（subject，可凹）被凸多边形（clip）裁剪
//
// ============================================================================
// 坐标系约定（重要）
// ----------------------------------------------------------------------------
// * 输入/输出坐标均为平面直角坐标系（笛卡尔坐标，单位由调用方自行解释，
//   例如米）。本程序不做任何投影/经纬度处理；如输入经纬度需先自行投影。
// * X 轴向右、Y 轴向上（数学约定）。叉积 > 0 表示“左/逆时针转向”。
// * 顶点绕向：CCW = 逆时针（面积符号为正），CW = 顺时针。
//   输出统一规范化为 CCW。
//
// 算法
// ----------------------------------------------------------------------------
// 被裁剪多边形允许为任意“简单多边形”（可凹、不可自交），裁剪多边形必须
// 严格凸。逐半平面的 Sutherland–Hodgman 在凹被裁多边形与同一条裁剪边有多
// 段交叠时会产生沿裁剪边的虚假“桥接”（环自交、面积虚增），因此这里采用
// Weiler–Atherton 式的“边相交 + 边界行走”：
//   1) 求两边界集合的所有相交（横截交点、共线重叠区间端点、接触点）；
//   2) 用这些点把两条边界各自细分；
//   3) 标记落在对方区域（含边界）内的子段；
//   4) 沿交集边界行走：在被裁边界上保持 CCW 前进，遇“穿出”点转到裁剪边界
//      上指向交集内部的方向，遇“穿入”点再转回被裁边界，直至闭合。
// 凸裁剪域与简单多边形的交集是单一弱简单环（可能退化为点/线段）。
//
// 数值与精度
// ----------------------------------------------------------------------------
// * 全部使用 IEEE-754 double。
// * 几何判定使用与数据尺度成比例的容差 tol = EPS_REL * max(1, 坐标范围)，
//   EPS_REL = 1e-10；同一几何点的不同数值解以 tol 网格吸附合并。
// * 交点参数 t 夹到 [0,1]，吸收数值噪声造成的越界。
//
// 退化处理
// ----------------------------------------------------------------------------
// * 自交/自触/共线重叠的被裁多边形：拒绝（SELF_INTERSECTING）。
//   相邻边允许共线（路径冗余点），但相邻边折返重叠拒绝。
// * 裁剪多边形必须严格凸（连续叉积同号非零、面积 > 0）。
// * 半平面/多边形内部判定为“含边界”，故落在裁剪边上的点保留；沿裁剪边
//   重合的边整体保留。
// * 输出环清理：吸附近重合点、删除环上连续共线的中间点。
// * 结果分类：
//     >=3 个有效顶点且面积显著  -> POLYGON（CCW 有序顶点）
//     面积退化、最远两点相距明显 -> SEGMENT（边界接触）
//     仅一个点簇                 -> POINT（角/边接触）
//     无任何边界                 -> EMPTY（无交集）
//   退化结果坐标同样保证落在裁剪闭区域内。
// ============================================================================
#pragma once

#include <algorithm>
#include <cmath>
#include <limits>
#include <map>
#include <string>
#include <utility>
#include <vector>

namespace geom {

constexpr double EPS_REL = 1e-10;

struct Point {
    double x = 0.0;
    double y = 0.0;
    Point() = default;
    Point(double x_, double y_) : x(x_), y(y_) {}
};

inline Point operator+(const Point& a, const Point& b) { return {a.x + b.x, a.y + b.y}; }
inline Point operator-(const Point& a, const Point& b) { return {a.x - b.x, a.y - b.y}; }
inline Point operator*(const Point& a, double s) { return {a.x * s, a.y * s}; }

inline double dot(const Point& a, const Point& b) { return a.x * b.x + a.y * b.y; }
inline double cross(const Point& a, const Point& b) { return a.x * b.y - a.y * b.x; }
inline double cross(const Point& a, const Point& b, const Point& c) {
    return cross(b - a, c - a);
}
inline double norm(const Point& a) { return std::hypot(a.x, a.y); }
inline double dist(const Point& a, const Point& b) { return norm(a - b); }

using Ring = std::vector<Point>;

enum class StatusCode {
    OK = 0,
    INVALID_SUBJECT = 1,
    SELF_INTERSECTING = 2,
    INVALID_CLIP = 3,
    EMPTY_OR_DEGENERATE_INPUT = 4,
};

enum class ResultKind { EMPTY, POINT, SEGMENT, POLYGON };

struct Result {
    StatusCode status = StatusCode::OK;
    std::string message;
    ResultKind kind = ResultKind::EMPTY;
    Ring ring;
    double signed_area = 0.0;
    double area = 0.0;
    double tol = 0.0;
};

inline double compute_scale(const Ring& a, const Ring& b) {
    double s = 1.0;
    for (const Ring* r : {&a, &b})
        for (const auto& p : *r)
            s = std::max({s, std::abs(p.x), std::abs(p.y)});
    return s;
}

inline double signed_area(const Ring& r) {
    double a = 0.0;
    for (size_t i = 0; i < r.size(); ++i)
        a += cross(r[i], r[(i + 1) % r.size()]);
    return a * 0.5;
}

inline double point_segment_dist(const Point& p, const Point& a, const Point& b) {
    Point ab = b - a;
    double len2 = dot(ab, ab);
    if (len2 <= 0.0) return dist(p, a);
    double t = std::clamp(dot(p - a, ab) / len2, 0.0, 1.0);
    return dist(p, a + ab * t);
}

// 线段关系，供自交校验
struct SegHit {
    bool proper = false;
    bool endpoint_touch = false;
    bool collinear_overlap = false;
    Point point;
};

inline SegHit segment_intersection(const Point& p1, const Point& p2,
                                   const Point& p3, const Point& p4, double tol) {
    SegHit hit;
    Point d1 = p2 - p1, d2 = p4 - p3;
    double l1 = norm(d1), l2 = norm(d2);
    double den = cross(d1, d2);
    Point r{p3.x - p1.x, p3.y - p1.y};

    if (std::abs(den) > tol * std::max(l1, l2)) {
        double t = cross(r, d2) / den;
        double u = cross(r, d1) / den;
        double eps = EPS_REL * std::max(1.0, std::max(l1, l2));
        if (t > -eps && t < 1.0 + eps && u > -eps && u < 1.0 + eps) {
            hit.point = p1 + d1 * std::clamp(t, 0.0, 1.0);
            if (t > eps && t < 1 - eps && u > eps && u < 1 - eps) hit.proper = true;
            else hit.endpoint_touch = true;
        }
        return hit;
    }
    if (std::abs(cross(d1, r)) > tol * std::max(l1, 1.0)) return hit;
    if (l1 == 0.0) {
        if (point_segment_dist(p1, p3, p4) <= tol) {
            hit.endpoint_touch = true;
            hit.point = p1;
        }
        return hit;
    }
    double t3 = dot(p3 - p1, d1) / (l1 * l1);
    double t4 = dot(p4 - p1, d1) / (l1 * l1);
    double lo = std::max(std::min(t3, t4), 0.0);
    double hi = std::min(std::max(t3, t4), 1.0);
    if (hi - lo > EPS_REL) {
        hit.collinear_overlap = true;
    } else if (hi - lo >= -EPS_REL) {
        hit.endpoint_touch = true;
        hit.point = p1 + d1 * std::clamp(0.5 * (lo + hi), 0.0, 1.0);
    }
    return hit;
}

inline bool is_simple_polygon(const Ring& r, double tol, std::string* reason) {
    const size_t n = r.size();
    for (size_t i = 0; i < n; ++i)
        if (dist(r[i], r[(i + 1) % n]) <= tol) {
            if (reason) *reason = "存在重合的相邻顶点（零长度边）";
            return false;
        }
    for (size_t i = 0; i < n; ++i) {
        size_t i2 = (i + 1) % n;
        for (size_t j = i + 1; j < n; ++j) {
            size_t j2 = (j + 1) % n;
            bool adjacent = (i2 == j) || (j2 == i);
            SegHit h = segment_intersection(r[i], r[i2], r[j], r[j2], tol);
            if (adjacent) {
                if (h.collinear_overlap) {
                    if (reason) *reason = "相邻边共线重叠（路径折返/自交）";
                    return false;
                }
                continue;
            }
            if (h.proper || h.endpoint_touch) {
                if (reason) *reason = "非相邻边相交或端点接触（自交/自触）";
                return false;
            }
            if (h.collinear_overlap) {
                if (reason) *reason = "非相邻边共线重叠（自交）";
                return false;
            }
        }
    }
    return true;
}

inline bool is_strictly_convex(const Ring& in, double tol, Ring* ccw_out,
                               std::string* reason) {
    Ring r = in;
    size_t n = r.size();
    if (n < 3) {
        if (reason) *reason = "裁剪多边形顶点数不足 3";
        return false;
    }
    double a = signed_area(r);
    if (std::abs(a) <= tol * tol) {
        if (reason) *reason = "裁剪多边形面积为 0（退化）";
        return false;
    }
    if (a < 0) std::reverse(r.begin(), r.end());
    int sign = 0;
    for (size_t i = 0; i < n; ++i) {
        double c = cross(r[i], r[(i + 1) % n], r[(i + 2) % n]);
        double escale = dist(r[i], r[(i + 1) % n]) *
                        dist(r[(i + 1) % n], r[(i + 2) % n]);
        double ltol = EPS_REL * std::max(1.0, escale);
        if (std::abs(c) <= ltol) {
            if (reason) *reason = "裁剪多边形存在共线连续顶点，不是严格凸";
            return false;
        }
        int s = c > 0 ? 1 : -1;
        if (sign == 0) sign = s;
        else if (sign != s) {
            if (reason) *reason = "裁剪多边形不是凸的（转向不一致）";
            return false;
        }
    }
    *ccw_out = std::move(r);
    return true;
}

// 点在凸多边形（CCW、含边界）
inline bool point_in_convex(const Point& p, const Ring& ccw, double tol) {
    for (size_t i = 0; i < ccw.size(); ++i)
        if (cross(ccw[(i + 1) % ccw.size()] - ccw[i], p - ccw[i]) < -tol)
            return false;
    return true;
}

// 点在简单多边形内（射线法，含边界容差）
inline bool point_in_simple(const Point& p, const Ring& r, double tol) {
    bool inside = false;
    size_t n = r.size();
    for (size_t i = 0, j = n - 1; i < n; j = i++) {
        if (point_segment_dist(p, r[j], r[i]) <= tol) return true;
        const Point& a = r[j];
        const Point& b = r[i];
        if ((a.y > p.y) != (b.y > p.y)) {
            double xcross = a.x + (b.x - a.x) * (p.y - a.y) / (b.y - a.y);
            if (xcross > p.x) inside = !inside;
        }
    }
    return inside;
}

// ===========================================================================
// Weiler–Atherton 式裁剪
// ===========================================================================
namespace detail {

// 两条线段的交点集合：横截 1 个；共线正长度重叠 2 个端点；端点接触 1 个。
inline std::vector<Point> edge_edge_points(const Point& a, const Point& b,
                                           const Point& c, const Point& d,
                                           double tol) {
    std::vector<Point> out;
    Point e1 = b - a, e2 = d - c;
    double l1 = norm(e1), l2 = norm(e2);
    double den = cross(e1, e2);
    Point r{c.x - a.x, c.y - a.y};
    double lenscale = std::max(l1, l2) * std::max(1.0, l1 * l2);

    if (std::abs(den) > tol * std::max(l1, l2)) {
        double t = cross(r, e2) / den;
        double u = cross(r, e1) / den;
        double eps = EPS_REL * std::max(1.0, std::max(l1, l2));
        if (t > -eps && t < 1 + eps && u > -eps && u < 1 + eps)
            out.push_back(a + e1 * std::clamp(t, 0.0, 1.0));
        return out;
    }
    // 平行
    if (std::abs(cross(e1, r)) > tol * std::max(l1, 1.0)) return out;
    if (l1 == 0.0) {
        if (point_segment_dist(a, c, d) <= tol) out.push_back(a);
        return out;
    }
    double tc = dot(c - a, e1) / (l1 * l1);
    double td = dot(d - a, e1) / (l1 * l1);
    double lo = std::max(std::min(tc, td), 0.0);
    double hi = std::min(std::max(tc, td), 1.0);
    if (hi - lo > EPS_REL) {
        out.push_back(a + e1 * lo);
        out.push_back(a + e1 * hi);
    } else if (hi - lo >= -EPS_REL * std::max(1.0, lenscale)) {
        out.push_back(a + e1 * std::clamp(0.5 * (lo + hi), 0.0, 1.0));
    }
    return out;
}

// tol 网格吸附键
inline std::pair<long long, long long> snap_key(const Point& p, double q) {
    return {std::llround(p.x / q), std::llround(p.y / q)};
}

struct SplitRing {
    Ring pts;                 // CCW 有序
    std::vector<bool> inside; // 子段 i: pts[i] -> pts[i+1] 中点在对方区域内
    std::vector<bool> shared; // pts[i] 是否为两边界公共点
};

// 用与另一环的所有交点细分 CCW 环
inline Ring split_ring(const Ring& r, const Ring& other, double tol,
                       std::map<std::pair<long long, long long>, int>* shared_keys,
                       double q) {
    Ring out;
    size_t n = r.size();
    for (size_t i = 0; i < n; ++i) {
        const Point& a = r[i];
        const Point& b = r[(i + 1) % n];
        Point e = b - a;
        double len2 = dot(e, e);
        struct Ev {
            double t;
            Point p;
        };
        std::vector<Ev> evs{{0.0, a}};
        for (size_t j = 0; j < other.size(); ++j) {
            auto ps = edge_edge_points(a, b, other[j], other[(j + 1) % other.size()], tol);
            for (Point p : ps) {
                double t = len2 > 0 ? dot(p - a, e) / len2 : 0.0;
                t = std::clamp(t, 0.0, 1.0);
                evs.push_back({t, a + e * t}); // 用 t 重投影，保证严格落在线上
                if (shared_keys) (*shared_keys)[snap_key(a + e * t, q)] = 1;
            }
        }
        std::sort(evs.begin(), evs.end(),
                  [](const Ev& x, const Ev& y) { return x.t < y.t; });
        for (size_t k = 0; k < evs.size(); ++k) {
            if (k + 1 < evs.size() &&
                std::abs(evs[k + 1].t - evs[k].t) <= EPS_REL)
                continue;
            Point p = evs[k].p;
            if (!out.empty() && dist(out.back(), p) < tol) continue;
            out.push_back(p);
        }
    }
    // 首尾若重合去掉尾
    while (out.size() > 1 && dist(out.front(), out.back()) < tol) out.pop_back();
    return out;
}

inline SplitRing build_split(const Ring& ccw, const Ring& other, double tol,
                             const std::map<std::pair<long long, long long>, int>& sk,
                             double q, bool other_is_convex) {
    SplitRing s;
    s.pts = ccw;
    size_t n = s.pts.size();
    s.inside.assign(n, false);
    s.shared.assign(n, false);
    for (size_t i = 0; i < n; ++i) {
        Point m = s.pts[i] * 0.5 + s.pts[(i + 1) % n] * 0.5;
        if (other_is_convex)
            s.inside[i] = point_in_convex(m, other, tol);
        else
            s.inside[i] = point_in_simple(m, other, tol);
        s.shared[i] = sk.find(snap_key(s.pts[i], q)) != sk.end();
    }
    return s;
}

// 环上近重复点合并
inline Ring dedup_ring(const Ring& r, double tol) {
    Ring out;
    for (const Point& p : r) {
        if (out.empty() || dist(out.back(), p) >= tol) out.push_back(p);
    }
    while (out.size() > 1 && dist(out.front(), out.back()) < tol) out.pop_back();
    return out;
}

// 删除环上连续共线的中间点
inline Ring decollinear(const Ring& r) {
    Ring out = r;
    bool changed = true;
    while (changed && out.size() >= 3) {
        changed = false;
        Ring nxt;
        size_t n = out.size();
        for (size_t i = 0; i < n; ++i) {
            const Point& a = out[(i + n - 1) % n];
            const Point& b = out[i];
            const Point& c = out[(i + 1) % n];
            double escale = dist(a, b) * dist(b, c);
            double ltol = EPS_REL * std::max(1.0, escale);
            if (std::abs(cross(a, b, c)) <= ltol && dot(b - a, c - b) >= -ltol)
                changed = true;
            else
                nxt.push_back(b);
        }
        out = std::move(nxt);
    }
    return out;
}

} // namespace detail

inline Result clip_polygon(const Ring& subject_in, const Ring& clip_in) {
    Result res;
    double scale = compute_scale(subject_in, clip_in);
    double tol = EPS_REL * scale;
    res.tol = tol;

    if (subject_in.size() < 3) {
        res.status = StatusCode::INVALID_SUBJECT;
        res.message = "被裁剪多边形至少需要 3 个顶点";
        return res;
    }
    Ring clip_ccw;
    std::string why;
    if (!is_strictly_convex(clip_in, tol, &clip_ccw, &why)) {
        res.status = StatusCode::INVALID_CLIP;
        res.message = why;
        return res;
    }
    std::string reason;
    if (!is_simple_polygon(subject_in, tol, &reason)) {
        res.status = StatusCode::SELF_INTERSECTING;
        res.message = reason;
        return res;
    }
    Ring subj = subject_in;
    if (signed_area(subj) < 0) std::reverse(subj.begin(), subj.end());

    // ---- 快速路径：全部顶点都在裁剪闭区域内 ----
    bool all_subj_in = true;
    for (const Point& p : subj)
        if (!point_in_convex(p, clip_ccw, tol)) { all_subj_in = false; break; }
    if (all_subj_in) {
        Ring r = detail::dedup_ring(subj, tol);
        r = detail::decollinear(r);
        res.kind = ResultKind::POLYGON;
        res.ring = r;
        res.signed_area = std::abs(signed_area(r));
        res.area = res.signed_area;
        res.message = "被裁多边形完全包含于裁剪域";
        return res;
    }

    double q = std::max(tol, scale * 1e-12);
    std::map<std::pair<long long, long long>, int> shared_keys;
    Ring sp = detail::split_ring(subj, clip_ccw, tol, &shared_keys, q);
    Ring cp = detail::split_ring(clip_ccw, subj, tol, &shared_keys, q);
    sp = detail::dedup_ring(sp, tol);
    cp = detail::dedup_ring(cp, tol);

    // 重新计算 shared_keys（dedup 后坐标仍来自同一投影，吸附一致）
    detail::SplitRing S = detail::build_split(sp, clip_ccw, tol, shared_keys, q, true);
    detail::SplitRing C = detail::build_split(cp, subj, tol, shared_keys, q, false);

    auto seg_id = [](size_t n, size_t i) {
        size_t j = (i + 1) % n;
        return std::make_pair(std::min(i, j), std::max(i, j));
    };

    // 起始：任一未使用的“中点在对方区域内”的 CCW 子段
    std::map<std::pair<size_t, size_t>, bool> used_s, used_c;
    int ring_id = -1;
    size_t start_idx = 0;
    for (size_t i = 0; i < S.pts.size(); ++i)
        if (S.inside[i] && !used_s[seg_id(S.pts.size(), i)]) {
            ring_id = 0; start_idx = i; break;
        }
    if (ring_id < 0) {
        for (size_t i = 0; i < C.pts.size(); ++i)
            if (C.inside[i] && !used_c[seg_id(C.pts.size(), i)]) {
                ring_id = 1; start_idx = i; break;
            }
    }

    if (ring_id < 0) {
        // 没有任何内部子段：仅可能点接触
        bool have = false;
        Point single{0, 0}, best1, best2;
        double bestd = 0;
        for (const Point& p : S.pts) {
            if (!point_in_convex(p, clip_ccw, tol)) continue;
            if (!have) { single = p; have = true; }
            for (const Point& q2 : S.pts)
                if (point_in_convex(q2, clip_ccw, tol) && dist(p, q2) > bestd) {
                    bestd = dist(p, q2); best1 = p; best2 = q2;
                }
        }
        if (!have) {
            res.kind = ResultKind::EMPTY;
            res.message = "无交集";
            return res;
        }
        if (bestd > tol) {
            res.kind = ResultKind::SEGMENT;
            res.ring = {best1, best2};
            res.message = "交集退化为线段（边界接触）";
        } else {
            res.kind = ResultKind::POINT;
            res.ring = {single};
            res.message = "交集退化为点（角/边接触）";
        }
        return res;
    }

    // ---- 边界行走（两环均为 CCW）----
    // 交集边界由两类弧交替构成：被裁环位于裁剪域内的弧、裁剪环位于被裁多边形
    // 内的弧，二者都沿 CCW。规则：尽量沿当前环 CCW 前进；当前子段在对方区域
    // 外或已使用时，在公共点换到另一环；两环都走不下去即闭合。
    auto locate = [](const detail::SplitRing& R, const Point& p, double tq) -> long long {
        auto key = detail::snap_key(p, tq);
        for (size_t i = 0; i < R.pts.size(); ++i)
            if (detail::snap_key(R.pts[i], tq) == key) return (long long)i;
        return -1;
    };

    Ring boundary;
    size_t node = start_idx;
    int start_ring = ring_id;
    boundary.push_back(ring_id == 0 ? S.pts[node] : C.pts[node]);

    int max_steps = (int)(S.pts.size() + C.pts.size()) * 4 + 16;
    for (int guard = 0; guard < max_steps; ++guard) {
        detail::SplitRing& R = ring_id == 0 ? S : C;
        auto& used = ring_id == 0 ? used_s : used_c;
        size_t nR = R.pts.size();
        if (R.inside[node] && !used[seg_id(nR, node)]) {
            used[seg_id(nR, node)] = true;
            node = (node + 1) % nR;
            boundary.push_back(R.pts[node]);
            if (ring_id == start_ring && node == start_idx) break; // 回到起点
            continue;
        }
        // 当前环走不通：在该公共点尝试换环
        Point here = R.pts[node];
        int other = ring_id == 0 ? 1 : 0;
        detail::SplitRing& O = other == 0 ? S : C;
        auto& used_o = other == 0 ? used_s : used_c;
        long long j = locate(O, here, q);
        if (j >= 0 && O.inside[(size_t)j] && !used_o[seg_id(O.pts.size(), (size_t)j)]) {
            ring_id = other;
            node = (size_t)j;
            // 同一几何点，不重复压入
        } else {
            break;
        }
    }

    Ring clean = detail::dedup_ring(boundary, tol);
    if (clean.size() >= 3) clean = detail::decollinear(clean);
    clean = detail::dedup_ring(clean, tol);

    double sa = clean.size() >= 3 ? signed_area(clean) : 0.0;
    double area_tol = EPS_REL * scale * scale;
    if (clean.size() >= 3 && std::abs(sa) > area_tol) {
        if (sa < 0) std::reverse(clean.begin(), clean.end());
        res.kind = ResultKind::POLYGON;
        res.ring = clean;
        res.signed_area = std::abs(sa);
        res.area = res.signed_area;
        res.message = "多边形裁剪成功";
        return res;
    }
    // 退化：找最远两点
    double bestd = 0;
    Point b1, b2;
    for (size_t i = 0; i < clean.size(); ++i)
        for (size_t j = i + 1; j < clean.size(); ++j)
            if (dist(clean[i], clean[j]) > bestd) {
                bestd = dist(clean[i], clean[j]);
                b1 = clean[i]; b2 = clean[j];
            }
    if (bestd > tol) {
        res.kind = ResultKind::SEGMENT;
        res.ring = {b1, b2};
        res.message = "交集退化为线段（边界接触）";
        return res;
    }
    if (!clean.empty()) {
        Point c{0, 0};
        for (const Point& p : clean) c = c + p;
        c = c * (1.0 / clean.size());
        res.kind = ResultKind::POINT;
        res.ring = {c};
        res.message = "交集退化为点（角/边接触）";
        return res;
    }
    res.kind = ResultKind::EMPTY;
    res.message = "无交集";
    return res;
}

} // namespace geom
