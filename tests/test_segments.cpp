// SPDX-License-Identifier: MIT
// C++ 内嵌测试：BigInt 数值层 + 几何分类的固定用例、大坐标、零长、端点交换性质。
// 随机小坐标对拍由 tests/crosscheck.py（Python Fraction 参考实现）负责。
#include "segi/bigint.hpp"
#include "segi/geometry.hpp"
#include "segi/rational.hpp"

#include <cstdint>
#include <cstdio>
#include <functional>
#include <string>

using namespace segi;

static int g_failures = 0;
static int g_checks = 0;

#define CHECK(cond)                                                                 \
    do {                                                                            \
        ++g_checks;                                                                 \
        if (!(cond)) {                                                              \
            ++g_failures;                                                           \
            std::printf("FAIL %s:%d  %s\n", __FILE__, __LINE__, #cond);            \
        }                                                                           \
    } while (0)

#define CHECK_EQ_STR(got, want)                                                     \
    do {                                                                            \
        ++g_checks;                                                                 \
        std::string g_ = (got);                                                     \
        if (g_ != (want)) {                                                         \
            ++g_failures;                                                           \
            std::printf("FAIL %s:%d  got=%s want=%s\n", __FILE__, __LINE__,        \
                        g_.c_str(), (want));                                         \
        }                                                                           \
    } while (0)

namespace {

using BI = BigInt;

void testBigInt() {
    // 基本转换
    CHECK(BI(0).to_string() == "0");
    CHECK(BI(-1).to_string() == "-1");
    CHECK(BI(INT64_MIN).to_string() == "-9223372036854775808");
    CHECK(BI(INT64_MAX).to_string() == "9223372036854775807");

    // parse / to_string 往返
    const char* nums[] = {
        "0", "1", "-1", "1234567890", "-98765432109876543210",
        "9223372036854775808", "-9223372036854775809",
        "1234567890123456789012345678901234567890",
        "-555555555555555555555555555555555555555555555555"
    };
    for (const char* s : nums) CHECK(BI::parse(s).to_string() == s);

    // 加减乘
    CHECK((BI(INT64_MAX) + BI(1)).to_string() == "9223372036854775808");
    CHECK((BI(INT64_MIN) - BI(1)).to_string() == "-9223372036854775809");
    CHECK((BI(INT64_MAX) * BI(INT64_MAX)).to_string() ==
          "85070591730234615847396907784232501249");
    CHECK((BI(-INT64_MAX) * BI(INT64_MAX)).to_string() ==
          "-85070591730234615847396907784232501249");
    CHECK(BI(123456789) + BI(-987654321) == BI(-864197532));
    CHECK(BI(5) - BI(10) == BI(-5));
    CHECK(BI(-5) - BI(-10) == BI(5));

    // 除/模（向零截断，余数跟随被除数）
    CHECK(BI(17) / BI(5) == BI(3));
    CHECK(BI(17) % BI(5) == BI(2));
    CHECK(BI(-17) / BI(5) == BI(-3));
    CHECK(BI(-17) % BI(5) == BI(-2));
    CHECK(BI(17) / BI(-5) == BI(-3));
    CHECK(BI(17) % BI(-5) == BI(2));
    CHECK((BI::parse("100000000000000000000") / BI(7)).to_string() == "14285714285714285714");
    CHECK((BI::parse("100000000000000000000") % BI(7)).to_string() == "2");

    // gcd
    CHECK(BI::gcd(BI(0), BI(12)) == BI(12));
    CHECK(BI::gcd(BI(12), BI(0)) == BI(12));
    CHECK(BI::gcd(BI(BI::parse("999999999999")), BI(BI::parse("123456789"))) == BI(9));
    CHECK(BI::gcd(BI(-48), BI(18)) == BI(6));

    // 比较
    CHECK(BI::parse("-10000000000000000000") < BI(1));
    CHECK(BI(99) < BI::parse("10000000000000000000"));
    CHECK(!(BI(0) < BI(0)));
    CHECK(BI(0) == BI(0));
    CHECK(BI(-1) < BI(0));

    // 有理数
    Rat a(BI(2), BI(4));
    CHECK(a.n == BI(1) && a.d == BI(2));
    Rat b(BI(1), BI(3));
    CHECK((a + b) == Rat(BI(5), BI(6)));
    CHECK((a * b) == Rat(BI(1), BI(6)));
    Rat c(BI(-3), BI(-9));
    CHECK(c.n == BI(1) && c.d == BI(3));
    CHECK(Rat(BI(-1), BI(2)) < Rat(BI(1), BI(3)));
}

Point pt(int64_t x, int64_t y) { return {BI(x), BI(y)}; }
Segment seg(int64_t x1, int64_t y1, int64_t x2, int64_t y2) {
    return {pt(x1, y1), pt(x2, y2)};
}

void expectRel(const Segment& a, const Segment& b, RelType want, const std::string& name) {
    IntersectionResult r = classify(a, b);
    if (r.type != want) {
        ++g_failures;
        std::printf("FAIL geometry [%s]: got=%s want=%s\n",
                    name.c_str(), relName(r.type), relName(want));
    }
    ++g_checks;
}

// 校验单点交点坐标
void expectPoint(const Segment& a, const Segment& b,
                 int64_t nx, int64_t ny, int64_t den, const std::string& name) {
    IntersectionResult r = classify(a, b);
    ++g_checks;
    Rat X(nx, den), Y(ny, den);
    if (!(r.point.x == X && r.point.y == Y)) {
        ++g_failures;
        std::printf("FAIL point [%s]: got=(%s,%s) want=(%s,%s)\n", name.c_str(),
                    r.point.x.str().c_str(), r.point.y.str().c_str(),
                    X.str().c_str(), Y.str().c_str());
    }
}

Segment swapEnds(const Segment& s) { return {s.q, s.p}; }

// 端点交换不变性（端点可能互换，结果关系与几何点必须一致）
void assertSwapInvariance(const Segment& a, const Segment& b) {
    IntersectionResult r[4] = {
        classify(a, b),
        classify(swapEnds(a), b),
        classify(a, swapEnds(b)),
        classify(swapEnds(a), swapEnds(b)),
    };
    auto samePoint = [](const IntersectionResult& u, const IntersectionResult& v) {
        if (u.type == RelType::COLLINEAR_OVERLAP)
            return u.overlapStart.x == v.overlapStart.x && u.overlapStart.y == v.overlapStart.y &&
                   u.overlapEnd.x == v.overlapEnd.x && u.overlapEnd.y == v.overlapEnd.y;
        if (u.type == RelType::DISJOINT) return true;
        return u.point.x == v.point.x && u.point.y == v.point.y;
    };
    for (int k = 1; k < 4; ++k) {
        ++g_checks;
        if (r[k].type != r[0].type || !samePoint(r[k], r[0])) {
            ++g_failures;
            std::printf("FAIL swap invariance: types %s vs %s\n",
                        relName(r[0].type), relName(r[k].type));
        }
    }
}

void testCrossAndTouch() {
    // 标准交叉（内部×内部）：(0,0)-(2,2) 与 (0,2)-(2,0) -> (1,1)
    expectRel(seg(0,0,2,2), seg(0,2,2,0), RelType::CROSS, "X cross");
    expectPoint(seg(0,0,2,2), seg(0,2,2,0), 1, 1, 1, "X cross pt");

    // 有理交点 (1/3,1/3)：A(0,0)-(3,3) 不过 (1,1) 整数... 设计：
    // A (0,0)-(3,0) 水平，B (1,-3)-(1,3) 竖直 -> (1,0) 整数，换一个：
    // A (0,0)-(2,2): s；B (0,3)-(3,0): 交在 (1,1) 整数。
    // 真有理：A (0,0)-(4,2) 与 B (0,2)-(4,0)：解 4s? 求交：
    //   A: (4t,2t)，B: (4u,2-2u) => 2t=2-2u,4t=4u => t=u=1/2 -> (2,1) 整数。
    // 用 A (0,0)-(3,1) 与 B (0,2)-(2,0)：
    //   (3t,t)=(2u,2-2u) => 3t=2u, t=2-2u => u=3t/2, t=2-3t => t=1/2, u=3/4
    //   点 (3/2,1/2)
    expectPoint(seg(0,0,3,1), seg(0,2,2,0), 3, 1, 2, "rational cross");
    expectRel(seg(0,0,3,1), seg(0,2,2,0), RelType::CROSS, "rational cross rel");

    // T 接：B 的端点落在 A 内部
    expectRel(seg(0,0,4,0), seg(2,0,2,3), RelType::ENDPOINT_TOUCH, "T touch");
    expectPoint(seg(0,0,4,0), seg(2,0,2,3), 2, 0, 1, "T touch pt");

    // 端点对端点
    expectRel(seg(0,0,1,0), seg(1,0,2,0), RelType::ENDPOINT_TOUCH, "end-end");
    {
        IntersectionResult r = classify(seg(0,0,1,0), seg(1,0,2,0));
        CHECK(r.flags.onA == "q");
        CHECK(r.flags.onB == "p");
    }

    // 内部×内部但延长线交在线段外 -> disjoint
    expectRel(seg(0,0,1,0), seg(2,2,3,3), RelType::DISJOINT, "miss1");
    expectRel(seg(0,0,1,0), seg(5,1,6,2), RelType::DISJOINT, "miss2");
    expectRel(seg(0,0,1,1), seg(2,0,3,1), RelType::DISJOINT, "parallel miss");

    // 非共线但共端点不同方向，以及两线共端点但 B 指向反侧
    expectRel(seg(0,0,1,1), seg(0,0,-1,1), RelType::ENDPOINT_TOUCH, "shared p");
}

void testCollinear() {
    // 重叠 [1,3]
    expectRel(seg(0,0,4,0), seg(1,0,3,0), RelType::COLLINEAR_OVERLAP, "contained overlap");
    {
        IntersectionResult r = classify(seg(0,0,4,0), seg(1,0,3,0));
        CHECK(r.overlapStart.x == Rat(1, 1));
        CHECK(r.overlapEnd.x == Rat(3, 1));
    }
    // 相等线段
    expectRel(seg(0,0,4,0), seg(4,0,0,0), RelType::COLLINEAR_OVERLAP, "equal reversed");
    // 部分重叠
    expectRel(seg(0,0,2,0), seg(1,0,3,0), RelType::COLLINEAR_OVERLAP, "partial");
    {
        IntersectionResult r = classify(seg(0,0,2,0), seg(1,0,3,0));
        CHECK(r.overlapStart.x == Rat(1, 1));
        CHECK(r.overlapEnd.x == Rat(2, 1));
    }
    // 共线仅端点相接（退化重叠为点）-> endpoint_touch
    expectRel(seg(0,0,2,0), seg(2,0,4,0), RelType::ENDPOINT_TOUCH, "col point touch");
    {
        IntersectionResult r = classify(seg(0,0,2,0), seg(2,0,4,0));
        CHECK(r.flags.onA == "q");
        CHECK(r.flags.onB == "p");
        CHECK(r.point.x == Rat(2, 1));
    }
    // 共线不相交（有间隙）
    expectRel(seg(0,0,2,0), seg(3,0,5,0), RelType::DISJOINT, "col gap");
    // 斜向共线：点沿 (2,2) 方向
    // A (0,0)-(4,4)；B (2,2)-(6,6) 重叠 (2,2)-(4,4)
    {
        IntersectionResult r = classify(seg(0,0,4,4), seg(2,2,6,6));
        CHECK(r.type == RelType::COLLINEAR_OVERLAP);
        CHECK(r.overlapStart.x == Rat(2, 1) && r.overlapStart.y == Rat(2, 1));
        CHECK(r.overlapEnd.x == Rat(4, 1) && r.overlapEnd.y == Rat(4, 1));
    }
    // 垂直共线
    expectRel(seg(0,0,0,4), seg(0,-2,0,2), RelType::COLLINEAR_OVERLAP, "vertical overlap");
}

void testZeroLength() {
    // 零长（点）在线段内部
    expectRel(seg(2,0,2,0), seg(0,0,4,0), RelType::ENDPOINT_TOUCH, "point inside");
    // 点在线段端点
    expectRel(seg(0,0,0,0), seg(0,0,4,0), RelType::ENDPOINT_TOUCH, "point at end");
    {
        IntersectionResult r = classify(seg(0,0,0,0), seg(0,0,4,0));
        CHECK(r.aZero);
        CHECK(!r.bZero);
        CHECK(r.flags.onA == "p");
        CHECK(r.flags.onB == "p");
    }
    // 点不在线上
    expectRel(seg(1,1,1,1), seg(0,0,4,0), RelType::DISJOINT, "point off line");
    // 点在线所在直线上但超出范围
    expectRel(seg(5,0,5,0), seg(0,0,4,0), RelType::DISJOINT, "point beyond");
    // 两个零长：重合 / 不重合
    expectRel(seg(3,3,3,3), seg(3,3,3,3), RelType::ENDPOINT_TOUCH, "point=point");
    expectRel(seg(3,3,3,3), seg(3,4,3,4), RelType::DISJOINT, "point!=point");
    // 退化点碰斜线内部
    expectRel(seg(2,2,2,2), seg(0,0,4,4), RelType::ENDPOINT_TOUCH, "point on diagonal");
}

void testLargeCoordinates() {
    // 接近 ±2^63 的坐标：任何 64 位乘积都会溢出
    const int64_t M = INT64_MAX;
    const int64_t L = INT64_MIN + 1;
    // A (L,L)-(M,M) 对角线；B (L,M)-(M,L) 反对角 -> 交于 (0,0)
    Segment a = seg(L, L, M, M);
    Segment b = seg(L, M, M, L);
    expectRel(a, b, RelType::CROSS, "huge cross");
    expectPoint(a, b, 0, 0, 1, "huge cross origin");
    assertSwapInvariance(a, b);

    // 大坐标水平/竖直 T 接：A (L,0)-(M,0)，B (0,0)-(0,1)
    Segment c = seg(L, 0, M, 0);
    Segment d = seg(0, 0, 0, 1);
    expectRel(c, d, RelType::ENDPOINT_TOUCH, "huge T");
    assertSwapInvariance(c, d);

    // 超过 64 位的坐标（BigInt 直接解析十进制）
    Point P{BI::parse("100000000000000000000"), BI::parse("200000000000000000000")};
    Point Q{BI::parse("300000000000000000000"), BI::parse("400000000000000000000")};
    Segment e{P, Q};
    Segment e2{
        Point{BI::parse("100000000000000000000"), BI::parse("200000000000000000000")},
        Point{BI::parse("300000000000000000000"), BI::parse("400000000000000000000")}
    };
    IntersectionResult r = classify(e, e2);
    CHECK(r.type == RelType::COLLINEAR_OVERLAP);

    // 大坐标共线间隙判定（间隙 2，量级在 2^100）
    Segment g1{Point{BI::parse("1267650600228229401496703205376"), BI(0)},
               Point{BI::parse("2535301200456458802993406410752"), BI(0)}}; // 2^100 .. 2^101
    Segment g2{Point{BI::parse("2535301200456458802993406410754"), BI(0)},
               Point{BI::parse("5070602400912917605986812821504"), BI(0)}};
    CHECK(classify(g1, g2).type == RelType::DISJOINT);
}

// 简单确定性伪随机，覆盖端点交换
void testRandomSwapInvariance() {
    uint64_t s = 0x123456789abcdefULL;
    auto rnd = [&]() {
        s ^= s << 13; s ^= s >> 7; s ^= s << 17;
        return int64_t(s & 0xFFFF) - 32768; // [-32768, 32767]
    };
    for (int k = 0; k < 3000; ++k) {
        Segment a = seg(rnd(), rnd(), rnd(), rnd());
        Segment b = seg(rnd(), rnd(), rnd(), rnd());
        assertSwapInvariance(a, b);
    }
    // 强制部分零长
    for (int k = 0; k < 1000; ++k) {
        Segment a = seg(rnd(), rnd(), rnd(), rnd());
        Segment b = seg(rnd(), rnd(), rnd(), rnd());
        if (k & 1) a.q = a.p;
        if (k & 2) b.q = b.p;
        assertSwapInvariance(a, b);
    }
}

} // namespace

int main() {
    testBigInt();
    testCrossAndTouch();
    testCollinear();
    testZeroLength();
    testLargeCoordinates();
    testRandomSwapInvariance();

    if (g_failures == 0) {
        std::printf("C++ tests OK: %d checks passed\n", g_checks);
        return 0;
    }
    std::printf("C++ tests FAILED: %d / %d checks\n", g_failures, g_checks);
    return 1;
}
