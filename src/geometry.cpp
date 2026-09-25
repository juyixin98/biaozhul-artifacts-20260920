// geometry.cpp - 几何原语、输入校验与 Sutherland-Hodgman 凸裁剪实现
#include "geometry.hpp"

#include <algorithm>
#include <cmath>

namespace geom {

namespace {

constexpr long double kMinScale = 1.0L;

long double dist2(const Point& a, const Point& b) {
    long double dx = b.x - a.x, dy = b.y - a.y;
    return dx * dx + dy * dy;
}

long double edgeLen(const Point& a, const Point& b) {
    return sqrtl(dist2(a, b));
}

long double ringScale(const std::vector<Point>& p) {
    long double m = kMinScale;
    for (size_t i = 0; i < p.size(); ++i) {
        m = std::max(m, edgeLen(p[i], p[(i + 1) % p.size()]));
    }
    return m;
}

// 垂直距离容差(以叉积阈值表达): tol * max(1, 参考长度)
long double crossTol(long double tol, long double refLen) {
    return tol * std::max(kMinScale, refLen);
}

bool pointOnSegment(const Point& p, const Point& a, const Point& b,
                    const Eps& eps) {
    long double len = edgeLen(a, b);
    if (fabsl(cross(a, b, p)) > crossTol(eps.linear, len)) return false;
    long double t = eps.linear * std::max(kMinScale, len);
    return p.x >= std::min(a.x, b.x) - t && p.x <= std::max(a.x, b.x) + t &&
           p.y >= std::min(a.y, b.y) - t && p.y <= std::max(a.y, b.y) + t;
}

struct SegHit {
    bool hit = false;       // 任意相交(含端点接触、共线重叠)
    bool proper = false;    // 严格内点相交
    bool overlap = false;   // 共线且正长度重叠
    Point pt{};
};

// 两线段相交判定。tol 为线性容差(按边长自适应缩放)。
SegHit segmentIntersect(const Point& a, const Point& b, const Point& c,
                        const Point& d, const Eps& eps) {
    SegHit r;
    long double rx = b.x - a.x, ry = b.y - a.y;
    long double sx = d.x - c.x, sy = d.y - c.y;
    long double den = rx * sy - ry * sx;
    long double lenr = edgeLen(a, b), lens = edgeLen(c, d);
    long double scale = std::max({kMinScale, lenr, lens});
    long double dt = eps.linear * scale;

    auto collinearOverlap = [&]() -> bool {
        // 共线情形: 任一端点落在对方线段上即视为接触。
        return pointOnSegment(c, a, b, eps) ||
               pointOnSegment(d, a, b, eps) ||
               pointOnSegment(a, c, d, eps) ||
               pointOnSegment(b, c, d, eps);
    };

    // 将点投影到 r 方向的主轴上, 用于判断共线区间重叠长度。
    auto proj = [&](const Point& p) {
        return std::fabs(rx) >= std::fabs(ry) ? p.x : p.y;
    };

    if (fabsl(den) <= eps.linear * std::max(kMinScale, lenr * lens)) {
        // 平行: 仅当共线且重叠才算交。
        if (fabsl(cross(a, b, c)) <= crossTol(eps.linear, scale) &&
            collinearOverlap()) {
            r.hit = true;
            // 正长度重叠判定(不止一点接触): 投影区间交长度 > dt。
            long double lo1 = std::min(proj(a), proj(b));
            long double hi1 = std::max(proj(a), proj(b));
            long double lo2 = std::min(proj(c), proj(d));
            long double hi2 = std::max(proj(c), proj(d));
            long double ov = std::min(hi1, hi2) - std::max(lo1, lo2);
            r.overlap = ov > dt;
            if (r.overlap) {
                // 重叠区间中点(投影回主轴所在坐标, 另一坐标取线段端点均值)。
                long double mid = (std::max(lo1, lo2) + std::min(hi1, hi2)) / 2;
                if (std::fabs(rx) >= std::fabs(ry))
                    r.pt = {mid, (a.y + b.y) / 2};
                else
                    r.pt = {(a.x + b.x) / 2, mid};
            }
        }
        return r;
    }

    long double qpx = c.x - a.x, qpy = c.y - a.y;
    long double t = (qpx * sy - qpy * sx) / den;
    long double u = (qpx * ry - qpy * rx) / den;
    long double tt = dt / std::max(lenr, kMinScale);
    long double ut = dt / std::max(lens, kMinScale);
    if (t < -tt || t > 1 + tt || u < -ut || u > 1 + ut) return r;

    long double tc = std::clamp(t, 0.0L, 1.0L);
    r.hit = true;
    r.pt = {a.x + tc * rx, a.y + tc * ry};
    r.proper = (t > tt && t < 1 - tt && u > ut && u < 1 - ut);
    return r;
}

// 循环去除相邻重复点(含首尾)。
std::vector<Point> dedupeCyclic(const std::vector<Point>& p, const Eps& eps) {
    std::vector<Point> out;
    long double scale = ringScale(p);
    long double d2 = crossTol(eps.linear, scale) * crossTol(eps.linear, scale);
    for (const auto& q : p) {
        if (out.empty() || dist2(out.back(), q) > d2) out.push_back(q);
    }
    while (out.size() > 1 && dist2(out.front(), out.back()) <= d2)
        out.pop_back();
    return out;
}

}  // namespace

// ---------------------------------------------------------------- 基本原语

long double cross(const Point& a, const Point& b, const Point& c) {
    return (b.x - a.x) * (c.y - a.y) - (b.y - a.y) * (c.x - a.x);
}

long double signedArea(const std::vector<Point>& poly) {
    long double s = 0;
    for (size_t i = 0; i < poly.size(); ++i) {
        const Point& a = poly[i];
        const Point& b = poly[(i + 1) % poly.size()];
        s += a.x * b.y - b.x * a.y;
    }
    return s * 0.5L;
}

bool pointInConvexCCW(const Point& p, const std::vector<Point>& poly,
                      const Eps& eps) {
    for (size_t i = 0; i < poly.size(); ++i) {
        const Point& a = poly[i];
        const Point& b = poly[(i + 1) % poly.size()];
        long double z = cross(a, b, p);
        if (z < -crossTol(eps.linear, edgeLen(a, b))) return false;
    }
    return true;
}

bool pointInPolygon(const Point& p, const std::vector<Point>& poly,
                    const Eps& eps) {
    bool inside = false;
    for (size_t i = 0, j = poly.size() - 1; i < poly.size(); j = i++) {
        const Point& a = poly[j];
        const Point& b = poly[i];
        if (pointOnSegment(p, a, b, eps)) return true;
        if ((a.y > p.y) != (b.y > p.y)) {
            long double xint = a.x + (p.y - a.y) * (b.x - a.x) / (b.y - a.y);
            if (xint > p.x) inside = !inside;
        }
    }
    return inside;
}

// ---------------------------------------------------------------- 校验

std::string validateSimplePolygon(const std::vector<Point>& polyIn,
                                  const char* name, const Eps& eps) {
    std::vector<Point> p = dedupeCyclic(polyIn, eps);
    size_t n = p.size();
    if (n < 3)
        return std::string(name) + " has fewer than 3 distinct vertices";

    long double scale = ringScale(p);
    long double dt = crossTol(eps.linear, scale);

    // 相邻边: 共线折点若位于两邻居之间属冗余点(允许, simplify 会删);
    // 若在线段外(回折尖刺 A-B-A 型)则拒绝。
    for (size_t i = 0; i < n; ++i) {
        const Point& a = p[(i + n - 1) % n];
        const Point& b = p[i];
        const Point& c = p[(i + 1) % n];
        if (fabsl(cross(a, b, c)) <= dt) {
            long double ac = edgeLen(a, c);
            long double ab = edgeLen(a, b), bc = edgeLen(b, c);
            if (ab + bc > ac + eps.linear * std::max(kMinScale, ac))
                return std::string(name) +
                       " has a backtracking spike (collinear vertex outside "
                       "its neighbors)";
        }
    }

    // 非相邻边: 任何相交或接触(含端点接触)都拒绝, 即只接受严格简单多边形。
    for (size_t i = 0; i < n; ++i) {
        const Point& a = p[i];
        const Point& b = p[(i + 1) % n];
        for (size_t j = i + 1; j < n; ++j) {
            bool adjacent = (j == i + 1) || (i == 0 && j == n - 1);
            if (adjacent) continue;
            const Point& c = p[j];
            const Point& d = p[(j + 1) % n];
            SegHit h = segmentIntersect(a, b, c, d, eps);
            if (h.hit)
                return std::string(name) +
                       " is self-intersecting (non-adjacent edges meet)";
        }
    }
    return "";
}

std::string validateConvexClip(const std::vector<Point>& poly, const Eps& eps) {
    size_t n = poly.size();
    if (n < 3) return "clip polygon must have at least 3 vertices";
    int sign = 0;
    for (size_t i = 0; i < n; ++i) {
        const Point& a = poly[(i + n - 1) % n];
        const Point& b = poly[i];
        const Point& c = poly[(i + 1) % n];
        long double z = cross(a, b, c);
        long double dt = crossTol(eps.linear,
                                  std::max(edgeLen(a, b), edgeLen(b, c)));
        if (fabsl(z) <= dt) continue;  // 冗余共线点(理论上已被 simplify 去掉)
        int s = z > 0 ? 1 : -1;
        if (sign == 0)
            sign = s;
        else if (s != sign)
            return "clip polygon is not convex";
    }
    if (sign == 0) return "clip polygon is degenerate (all points collinear)";
    return "";
}

// ---------------------------------------------------------------- 简化

std::vector<Point> simplifyRing(const std::vector<Point>& polyIn,
                                const Eps& eps) {
    std::vector<Point> p = dedupeCyclic(polyIn, eps);
    if (p.size() < 3) return p;

    bool changed = true;
    while (changed && p.size() >= 3) {
        changed = false;
        size_t n = p.size();
        long double scale = ringScale(p);
        long double dt = crossTol(eps.linear, scale);
        for (size_t i = 0; i < n; ++i) {
            const Point& a = p[(i + n - 1) % n];
            const Point& b = p[i];
            const Point& c = p[(i + 1) % n];
            // b 严格位于 a-c 之间且三点共线 -> 去掉冗余折点。
            if (fabsl(cross(a, b, c)) <= dt &&
                pointOnSegment(b, a, c, eps) &&
                dist2(a, b) > 0 && dist2(b, c) > 0 && dist2(a, c) > 0) {
                p.erase(p.begin() + static_cast<long>(i));
                changed = true;
                break;
            }
        }
    }
    return p;
}

// ---------------------------------------------------------------- 裁剪

namespace {

// 有符号侧别: >0 在有向边 a->b 左侧, ==0 在线上。
long double side(const Point& p, const Point& a, const Point& b) {
    return cross(a, b, p);
}

// 求 S-E 与裁剪边 a-b 的交点(调用保证两侧跨越且不平行)。
Point crossing(const Point& S, const Point& E, const Point& a,
               const Point& b, const Eps& eps) {
    long double rx = E.x - S.x, ry = E.y - S.y;
    long double dx = b.x - a.x, dy = b.y - a.y;
    long double den = rx * dy - ry * dx;
    if (fabsl(den) <= eps.linear *
                          std::max(kMinScale, edgeLen(S, E) * edgeLen(a, b))) {
        // 数值上平行(理论不该发生): 取两端点中更贴近裁剪边的一个。
        return fabsl(cross(a, b, S)) <= fabsl(cross(a, b, E)) ? S : E;
    }
    long double t = ((a.x - S.x) * dy - (a.y - S.y) * dx) / den;
    t = std::clamp(t, 0.0L, 1.0L);
    return {S.x + t * rx, S.y + t * ry};
}

// 防御性自检: 输出环是否出现非相邻边交叉/正长度重叠, 或相邻边回折
// (沿同一线段往返 -> 断连组件被零面积“桥”接在一起)。
// 凸集与简单多边形的交若为单个连通简单多边形则不应出现这些现象;
// 出现则说明结果无法用单个简单多边形表示。
std::string ringSelfContact(const std::vector<Point>& p, const Eps& eps) {
    size_t n = p.size();
    for (size_t i = 0; i < n; ++i) {
        // 相邻边: 正长度共线重叠(A-B 与 B-C 回折)或长度近零。
        const Point& a = p[i];
        const Point& b = p[(i + 1) % n];
        const Point& c = p[(i + 2) % n];
        SegHit adj = segmentIntersect(a, b, b, c, eps);
        if (adj.overlap)
            return "RESULT_MULTIPLE_COMPONENTS: clipping output backtracks "
                   "along a clip edge (intersection is not a single ring)";

        for (size_t j = i + 1; j < n; ++j) {
            bool adjacent = (j == i + 1) || (i == 0 && j == n - 1);
            if (adjacent) continue;
            SegHit h = segmentIntersect(p[i], p[(i + 1) % n], p[j],
                                        p[(j + 1) % n], eps);
            if (!h.hit) continue;
            if (h.proper)
                return "RESULT_NOT_SIMPLE: clipping output is "
                       "self-intersecting";
            // 非相邻边之间任何接触(含端点接触、共线重叠)都意味着夹捏/断连。
            return "RESULT_MULTIPLE_COMPONENTS: clipping output touches itself "
                   "(intersection cannot be represented as one simple ring)";
        }
    }
    return "";
}

}  // namespace

std::string clipPolygonByConvex(const std::vector<Point>& subjectIn,
                                const std::vector<Point>& clipIn,
                                const Eps& eps, ClipResult& out) {
    out = ClipResult{};

    std::string err = validateSimplePolygon(subjectIn, "subject", eps);
    if (!err.empty()) return "INVALID_SUBJECT: " + err;
    err = validateSimplePolygon(clipIn, "clip", eps);
    if (!err.empty()) return "INVALID_CLIP: " + err;

    std::vector<Point> subject = simplifyRing(subjectIn, eps);
    std::vector<Point> clip = simplifyRing(clipIn, eps);
    if (subject.size() < 3)
        return "INVALID_SUBJECT: subject degenerates to point/segment";
    if (clip.size() < 3)
        return "INVALID_CLIP: clip polygon is degenerate";
    err = validateConvexClip(clip, eps);
    if (!err.empty()) return "INVALID_CLIP: " + err;

    long double subArea = signedArea(subject);
    out.inputSubjectReversed = subArea < 0;

    // 统一裁剪多边形方向为 CCW。
    if (signedArea(clip) < 0) std::reverse(clip.begin(), clip.end());

    std::vector<Point> cur = subject;
    for (size_t e = 0; e < clip.size() && !cur.empty(); ++e) {
        const Point& a = clip[e];
        const Point& b = clip[(e + 1) % clip.size()];
        long double dt = crossTol(eps.linear, edgeLen(a, b));

        std::vector<Point> nxt;
        nxt.reserve(cur.size() + 2);
        Point S = cur.back();
        bool Sin = side(S, a, b) >= -dt;
        for (const Point& E : cur) {
            bool Ein = side(E, a, b) >= -dt;
            if (Ein) {
                if (!Sin) nxt.push_back(crossing(S, E, a, b, eps));
                nxt.push_back(E);
            } else if (Sin) {
                nxt.push_back(crossing(S, E, a, b, eps));
            }
            S = E;
            Sin = Ein;
        }
        cur = simplifyRing(nxt, eps);
    }

    long double scale = ringScale(clip);
    long double areaTol = eps.linear * scale * scale;

    if (cur.empty()) {
        out.kind = ResultKind::Empty;
        return "";
    }
    if (cur.size() == 1) {
        out.kind = ResultKind::Point;
        out.vertices = cur;
        return "";
    }
    if (cur.size() == 2) {
        out.kind = ResultKind::Segment;
        out.vertices = cur;
        return "";
    }

    long double a = signedArea(cur);
    if (fabsl(a) <= areaTol) {
        // 有顶点但面积为零: 点或线段(防御路径, 理论上前置分支已覆盖)。
        long double best2 = 0;
        size_t bi = 0, bj = 1;
        for (size_t i = 0; i < cur.size(); ++i)
            for (size_t j = i + 1; j < cur.size(); ++j) {
                long double d2v = dist2(cur[i], cur[j]);
                if (d2v > best2) best2 = d2v, bi = i, bj = j;
            }
        if (best2 <= crossTol(eps.linear, scale) *
                        crossTol(eps.linear, scale)) {
            out.kind = ResultKind::Point;
            out.vertices = {cur[bi]};
        } else {
            out.kind = ResultKind::Segment;
            out.vertices = {cur[bi], cur[bj]};
        }
        return "";
    }

    err = ringSelfContact(cur, eps);
    if (!err.empty()) return err;

    if (a < 0) std::reverse(cur.begin(), cur.end());
    // 反转后再简化一次并复核面积符号。
    cur = simplifyRing(cur, eps);
    if (cur.size() < 3 || fabsl(signedArea(cur)) <= areaTol)
        return "RESULT_MULTIPLE_COMPONENTS: output collapsed during cleanup";

    out.kind = ResultKind::Polygon;
    out.vertices = cur;
    out.area = fabsl(signedArea(cur));
    out.orientation = 1;

    // 细条(sliver)提示: 面积非零但极小(达到退化阈值的 1e3 倍带宽内)。
    long double perim = 0;
    for (size_t i = 0; i < cur.size(); ++i)
        perim += edgeLen(cur[i], cur[(i + 1) % cur.size()]);
    long double widthEst = 2 * out.area / perim;
    if (widthEst < 1000 * eps.linear * scale)
        out.warning = "result is a very thin sliver (estimated width " +
                      std::to_string(widthEst) + ")";
    return "";
}

const char* resultKindName(ResultKind k) {
    switch (k) {
        case ResultKind::Empty:
            return "empty";
        case ResultKind::Point:
            return "point";
        case ResultKind::Segment:
            return "segment";
        case ResultKind::Polygon:
            return "polygon";
    }
    return "unknown";
}

}  // namespace geom
