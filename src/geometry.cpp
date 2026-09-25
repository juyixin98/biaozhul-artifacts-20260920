// SPDX-License-Identifier: MIT
#include "segi/geometry.hpp"

#include <stdexcept>

namespace segi {

namespace {

struct Vec { BigInt x, y; };

Vec sub(const Point& a, const Point& b) { return {a.x - b.x, a.y - b.y}; }
BigInt cross(const Vec& u, const Vec& v) { return u.x * v.y - u.y * v.x; }
BigInt dot(const Vec& u, const Vec& v) { return u.x * v.x + u.y * v.y; }

// 点 c 在线段 ab 内部？（已保证共线且 ab 非零长）
bool strictlyInside(const Point& a, const Point& b, const Point& c) {
    Vec ab = sub(b, a), ac = sub(c, a);
    BigInt tNum = dot(ac, ab);
    if (tNum.isZero() || tNum.neg) return false;     // t <= 0
    BigInt len = dot(ab, ab);
    return tNum < len;                                // 0 < t < 1
}

// 整数点转有理点
RatPoint rat(const Point& p) { return {Rat(p.x, BigInt(1)), Rat(p.y, BigInt(1))}; }

// 有理点比较：先 x 后 y
int cmpPoint(const RatPoint& a, const RatPoint& b) {
    int c = Rat::cmp(a.x, b.x);
    return c != 0 ? c : Rat::cmp(a.y, b.y);
}

// 非退化情形（两条都非零长）的主判定
IntersectionResult classifyNonZero(const Segment& A, const Segment& B) {
    IntersectionResult r;
    Vec u = sub(A.q, A.p);
    Vec v = sub(B.q, B.p);
    Vec w = sub(B.p, A.p);
    BigInt D  = cross(u, v);
    BigInt sN = cross(w, v); // A.p + s*u = B.p + t*v  中 s 的分子
    BigInt tN = cross(w, u); // t 的分子

    if (D != BigInt(0)) {
        // 不平行：解 s = sN/D、t = tN/D。判断 0<=s,t<=1
        auto inUnit = [&](const BigInt& num) -> int {
            // 返回 -1 (num<0)、0 (内部 0<num/D<1)、1 (等于端点 0 或 D)、2 (>1)
            BigInt z(0);
            BigInt nn = num;
            bool negDen = D.neg;
            // 规范化到分母为正
            if (negDen) nn = -nn;
            BigInt den = D; den.neg = false;
            if (nn.isZero()) return 1;
            if (nn.neg) return -1;
            if (nn == den) return 1;
            return nn < den ? 0 : 2;
        };
        int ss = inUnit(sN), tt = inUnit(tN);
        if (ss < 0 || ss > 1 || tt < 0 || tt > 1) { r.type = RelType::DISJOINT; return r; }

        // 有理交点：X = A.p + (sN/D)*u
        Rat s(sN, D);
        RatPoint X{
            Rat(A.p.x, BigInt(1)) + s * Rat(u.x, BigInt(1)),
            Rat(A.p.y, BigInt(1)) + s * Rat(u.y, BigInt(1))
        };
        r.point = X;
        r.flags.onA = ss == 0 ? "i" : (sN.isZero() ? "p" : "q");
        r.flags.onB = tt == 0 ? "i" : (tN.isZero() ? "p" : "q");
        r.type = (ss == 0 && tt == 0) ? RelType::CROSS : RelType::ENDPOINT_TOUCH;
        return r;
    }

    // D == 0：平行
    if (cross(w, u) != BigInt(0)) { r.type = RelType::DISJOINT; return r; }

    // 共线。把四个端点投影到 u 方向：参数 k = dot(P-A.p, u)/|u|^2
    // 比较只需比较分子（分母 |u|^2 > 0）。
    BigInt u2 = dot(u, u);
    auto proj = [&](const Point& P) { return dot(sub(P, A.p), u); };
    BigInt kp(0);                 // A.p
    BigInt kq = u2;               // A.q
    BigInt kr = proj(B.p);        // B.p
    BigInt ks = proj(B.q);        // B.q

    BigInt lo = kr, hi = ks;      // B 的区间
    bool krLo = true;             // lo 当前是否对应 B.p（交换后可能翻转）
    if (lo > hi) {
        using std::swap;
        swap(lo, hi);
        krLo = false;
    }
    if (hi < kp || lo > kq) { r.type = RelType::DISJOINT; return r; }

    // 公共参数区间 [max(0,lo), min(u2,hi)]
    BigInt istart = kp < lo ? lo : kp;
    BigInt iend   = kq > hi ? hi : kq;

    auto pointAt = [&](const BigInt& k) -> RatPoint {
        return {
            Rat(A.p.x * u2 + k * u.x, u2),
            Rat(A.p.y * u2 + k * u.y, u2)
        };
    };

    if (istart == iend) {
        // 一维区间只在一点相接：ENDPOINT_TOUCH
        r.type = RelType::ENDPOINT_TOUCH;
        r.point = pointAt(istart);
        // A 侧：参数 0 -> p，u2 -> q，否则内部
        if (istart == kp) r.flags.onA = "p";
        else if (istart == kq) r.flags.onA = "q";
        else r.flags.onA = "i";
        // B 侧：lo 端点 / hi 端点 / 内部
        bool atLo = (istart == lo);
        bool atHi = (istart == hi);
        if (atLo) r.flags.onB = krLo ? "p" : "q";
        else if (atHi) r.flags.onB = krLo ? "q" : "p";
        else r.flags.onB = "i";
        return r;
    }

    // 真正的一维重叠
    r.type = RelType::COLLINEAR_OVERLAP;
    RatPoint sPt = pointAt(istart);
    RatPoint ePt = pointAt(iend);
    if (cmpPoint(sPt, ePt) > 0) {
        using std::swap;
        swap(sPt, ePt);
    }
    r.overlapStart = sPt;
    r.overlapEnd = ePt;
    return r;
}

} // namespace

const char* relName(RelType t) {
    switch (t) {
        case RelType::DISJOINT: return "disjoint";
        case RelType::CROSS: return "cross";
        case RelType::ENDPOINT_TOUCH: return "endpoint_touch";
        case RelType::COLLINEAR_OVERLAP: return "collinear_overlap";
    }
    return "unknown";
}

IntersectionResult classify(const Segment& A, const Segment& B) {
    bool az = (A.p.x == A.q.x && A.p.y == A.q.y);
    bool bz = (B.p.x == B.q.x && B.p.y == B.q.y);

    IntersectionResult r;
    r.aZero = az;
    r.bZero = bz;

    if (az || bz) {
        // 点 × 点
        if (az && bz) {
            if (A.p.x == B.p.x && A.p.y == B.p.y) {
                r.type = RelType::ENDPOINT_TOUCH;
                r.point = rat(A.p);
                r.flags = {"p", "p"};
            } else r.type = RelType::DISJOINT;
            return r;
        }
        // 一点一线段
        const Point& C = az ? A.p : B.p;
        const Segment& L = az ? B : A;
        Vec v = sub(L.q, L.p), w = sub(C, L.p);
        BigInt c = cross(v, w);
        bool on = c.isZero() && !dot(w, v).neg && dot(w, v) <= dot(v, v);
        if (!on) { r.type = RelType::DISJOINT; return r; }
        r.type = RelType::ENDPOINT_TOUCH;
        r.point = rat(C);
        if (az) {
            r.flags.onA = "p";
            r.flags.onB = strictlyInside(L.p, L.q, C) ? "i"
                         : (C.x == L.p.x && C.y == L.p.y ? "p" : "q");
        } else {
            r.flags.onB = "p";
            r.flags.onA = strictlyInside(L.p, L.q, C) ? "i"
                         : (C.x == L.p.x && C.y == L.p.y ? "p" : "q");
        }
        return r;
    }

    return classifyNonZero(A, B);
}

} // namespace segi
