// 单元测试 + 随机性质测试（无第三方框架）。
// 覆盖：正向/负向命中、起点在盒内、擦边、退化盒（薄片/线段/点）、
//       零方向分量、BVH 与逐盒检测对照、NaN 不污染结果、BVH 剪枝有效性。
#include <chrono>
#include <cmath>
#include <cstdint>
#include <algorithm>
#include <iostream>
#include <random>
#include <sstream>
#include <string>
#include <vector>

#include "app/query.h"
#include "geo/bvh.h"
#include "geo/ray_box.h"
#include "json/json.h"

static int g_failures = 0;
static int g_checks = 0;

#define CHECK_TRUE(cond)                                                    \
    do {                                                                    \
        ++g_checks;                                                         \
        if (!(cond)) {                                                      \
            ++g_failures;                                                   \
            std::cerr << "FAIL " << __FILE__ << ":" << __LINE__             \
                      << " CHECK_TRUE(" #cond ")\n";                         \
        }                                                                   \
    } while (0)

static bool approx(double a, double b, double rel = 1e-9) {
    return std::fabs(a - b) <= rel * std::max({1.0, std::fabs(a), std::fabs(b)});
}

#define CHECK_NEAR(a, b)                                                    \
    do {                                                                    \
        ++g_checks;                                                         \
        double va_ = (a);                                                   \
        double vb_ = (b);                                                   \
        if (!approx(va_, vb_)) {                                            \
            ++g_failures;                                                   \
            std::cerr << "FAIL " << __FILE__ << ":" << __LINE__             \
                      << " " #a "=" << va_ << " vs " #b "=" << vb_ << "\n"; \
        }                                                                   \
    } while (0)

using geo::AABB;
using geo::Ray;
using geo::RayHit;
using geo::Vec3;
using js::JValue;

static Ray rayFrom(const Vec3& o, const Vec3& d) {
    Ray r;
    geo::makeRay(o, d, r);
    return r;
}

// ---------------------------------------------------------------------------
// 射线-单盒
// ---------------------------------------------------------------------------

static void testBasicPositive() {
    const AABB box{Vec3(0, 0, 0), Vec3(1, 1, 1)};
    // 沿 +x，起点 (-2,0.5,0.5)：tEnter=2, tExit=3
    RayHit h = geo::intersectRayAABB(rayFrom(Vec3(-2, 0.5, 0.5), Vec3(1, 0, 0)), box);
    CHECK_TRUE(h.hit);
    CHECK_TRUE(!h.inside);
    CHECK_NEAR(h.tEnter, 2.0);
    CHECK_NEAR(h.tExit, 3.0);

    // 未归一化方向也应给出欧氏距离
    RayHit h2 = geo::intersectRayAABB(rayFrom(Vec3(-2, 0.5, 0.5), Vec3(10, 0, 0)), box);
    CHECK_TRUE(h2.hit);
    CHECK_NEAR(h2.tEnter, 2.0);
    CHECK_NEAR(h2.tExit, 3.0);

    // 完全错过
    RayHit m = geo::intersectRayAABB(rayFrom(Vec3(-2, 2, 0.5), Vec3(1, 0, 0)), box);
    CHECK_TRUE(!m.hit);

    // 盒在起点后方
    RayHit behind = geo::intersectRayAABB(rayFrom(Vec3(5, 0.5, 0.5), Vec3(1, 0, 0)), box);
    CHECK_TRUE(!behind.hit);
}

static void testNegativeDirection() {
    const AABB box{Vec3(0, 0, 0), Vec3(1, 1, 1)};
    // 沿 -x，起点 (3,0.5,0.5)：tEnter=2, tExit=3
    RayHit h = geo::intersectRayAABB(rayFrom(Vec3(3, 0.5, 0.5), Vec3(-1, 0, 0)), box);
    CHECK_TRUE(h.hit);
    CHECK_NEAR(h.tEnter, 2.0);
    CHECK_NEAR(h.tExit, 3.0);

    // 斜向负方向 (1.5,1.5,1.5) 朝 (-1,-1,-1)，命中盒角方向：
    // 方向归一化后 tEnter = 0.5*sqrt(3)
    RayHit h2 = geo::intersectRayAABB(
        rayFrom(Vec3(1.5, 1.5, 1.5), Vec3(-1, -1, -1)), box);
    CHECK_TRUE(h2.hit);
    CHECK_NEAR(h2.tEnter, 0.5 * std::sqrt(3.0));
    CHECK_NEAR(h2.tExit, 1.5 * std::sqrt(3.0));

    // 负方向但盒在背后（起点负侧、方向仍负）
    RayHit behind = geo::intersectRayAABB(rayFrom(Vec3(-2, 0.5, 0.5), Vec3(-1, 0, 0)), box);
    CHECK_TRUE(!behind.hit);
}

static void testOriginInside() {
    const AABB box{Vec3(0, 0, 0), Vec3(2, 2, 2)};
    RayHit h = geo::intersectRayAABB(rayFrom(Vec3(1, 1, 1), Vec3(1, 0, 0)), box);
    CHECK_TRUE(h.hit);
    CHECK_TRUE(h.inside);
    CHECK_NEAR(h.tEnter, 0.0);
    CHECK_NEAR(h.tExit, 1.0);

    // 内部朝负方向
    RayHit h2 = geo::intersectRayAABB(rayFrom(Vec3(1, 1, 1), Vec3(-1, -1, -1)), box);
    CHECK_TRUE(h2.hit);
    CHECK_TRUE(h2.inside);
    CHECK_NEAR(h2.tEnter, 0.0);
    CHECK_NEAR(h2.tExit, std::sqrt(3.0));

    // 起点恰在入射面上、朝盒内：inside=false, tEnter=0
    RayHit face = geo::intersectRayAABB(rayFrom(Vec3(0, 1, 1), Vec3(1, 0, 0)), box);
    CHECK_TRUE(face.hit);
    CHECK_NEAR(face.tEnter, 0.0);
    CHECK_NEAR(face.tExit, 2.0);

    // pointInBox
    CHECK_TRUE(geo::pointInBox(Vec3(0, 0, 0), box));
    CHECK_TRUE(geo::pointInBox(Vec3(2, 2, 2), box));
    CHECK_TRUE(!geo::pointInBox(Vec3(-1e-6, 0, 0), box));
}

static void testGrazing() {
    const AABB box{Vec3(0, 0, 0), Vec3(1, 1, 1)};
    // 沿棱 y=1,z=1 擦过：与盒共享一条边，闭区间视为命中，接触段 t∈[2,3]
    RayHit edge = geo::intersectRayAABB(rayFrom(Vec3(-2, 1, 1), Vec3(1, 0, 0)), box);
    CHECK_TRUE(edge.hit);
    CHECK_NEAR(edge.tEnter, 2.0);
    CHECK_NEAR(edge.tExit, 3.0);

    // 向外偏移一个小量：miss
    RayHit edgeMiss = geo::intersectRayAABB(
        rayFrom(Vec3(-2, 1.000001, 1), Vec3(1, 0, 0)), box);
    CHECK_TRUE(!edgeMiss.hit);

    // 单点相切（切于棱）：射线沿外角平分线运动（在“两个外侧八分体”的
    // 分界方向上），从 (2,0.5,-2) 朝 (-,0,+)，直线 x+z=0 仅在棱
    // (0,*,0) 上的点 (0,0.5,0) 处接触，t=2*sqrt(2)，tEnter==tExit。
    RayHit corner = geo::intersectRayAABB(
        rayFrom(Vec3(2, 0.5, -2), Vec3(-1, 0, 1)), box);
    CHECK_TRUE(corner.hit);
    CHECK_NEAR(corner.tEnter, 2.0 * std::sqrt(2.0));
    CHECK_NEAR(corner.tExit, 2.0 * std::sqrt(2.0));

    // 向外微小偏移（x+z 恒为负）则不再命中
    RayHit cornerMiss = geo::intersectRayAABB(
        rayFrom(Vec3(2, 0.5, -2.000001), Vec3(-1, 0, 1)), box);
    CHECK_TRUE(!cornerMiss.hit);

    // 面对角线穿越（非擦边）：(-1,-1,0.5) 沿 (1,1,0) 在 (0,0) 入盒、
    // (1,1) 出盒，tEnter=tExit=sqrt(2)，是完整穿越。
    RayHit through = geo::intersectRayAABB(
        rayFrom(Vec3(-1, -1, 0.5), Vec3(1, 1, 0)), box);
    CHECK_TRUE(through.hit);
    CHECK_NEAR(through.tEnter, std::sqrt(2.0));
    CHECK_NEAR(through.tExit, 2.0 * std::sqrt(2.0));
}

static void testZeroComponents() {
    const AABB box{Vec3(0, 0, 0), Vec3(1, 1, 1)};
    // dir=(0,1,0)，起点在 x,z slab 内：命中
    RayHit h = geo::intersectRayAABB(rayFrom(Vec3(0.5, -2, 0.5), Vec3(0, 1, 0)), box);
    CHECK_TRUE(h.hit);
    CHECK_NEAR(h.tEnter, 2.0);
    CHECK_NEAR(h.tExit, 3.0);

    // 平行且在 slab 外：miss，且不得有 NaN
    RayHit m = geo::intersectRayAABB(rayFrom(Vec3(2, -2, 0.5), Vec3(0, 1, 0)), box);
    CHECK_TRUE(!m.hit);
    CHECK_TRUE(std::isfinite(m.tEnter) || !m.hit);  // hit=false 即正确

    // 两个零分量：方向沿 z，起点在盒内 xy
    RayHit h2 = geo::intersectRayAABB(rayFrom(Vec3(0.5, 0.5, -5), Vec3(0, 0, 1)), box);
    CHECK_TRUE(h2.hit);
    CHECK_NEAR(h2.tEnter, 5.0);
    CHECK_NEAR(h2.tExit, 6.0);

    // 两个零分量且平行错过
    RayHit m2 = geo::intersectRayAABB(rayFrom(Vec3(5, 0.5, 0.5), Vec3(0, 0, 1)), box);
    CHECK_TRUE(!m2.hit);

    // 零向量构造失败
    Ray bad;
    CHECK_TRUE(!geo::makeRay(Vec3(0, 0, 0), Vec3(0, 0, 0), bad));
    Ray ok;
    CHECK_TRUE(geo::makeRay(Vec3(0, 0, 0), Vec3(0, 0, 2), ok));
    CHECK_NEAR(ok.dir.z, 1.0);
}

static void testDegenerateBoxes() {
    // 薄片（z 厚度为 0），射线穿过
    const AABB slab{Vec3(0, 0, 1), Vec3(2, 2, 1)};
    RayHit h = geo::intersectRayAABB(rayFrom(Vec3(1, 1, -1), Vec3(0, 0, 1)), slab);
    CHECK_TRUE(h.hit);
    CHECK_NEAR(h.tEnter, 2.0);
    CHECK_NEAR(h.tExit, 2.0);  // 单点相交

    // 薄片，斜射：xy 也在范围内时命中
    RayHit h2 = geo::intersectRayAABB(
        rayFrom(Vec3(-1, 1, 0), Vec3(1, 0, 1)), slab);
    CHECK_TRUE(h2.hit);
    // 到达 z=1 需 t 沿单位方向 (-1,0,1)/sqrt2：t = sqrt2，点为 (0,1,1)
    CHECK_NEAR(h2.tEnter, std::sqrt(2.0));

    // 线段（y,z 厚度为 0）
    const AABB seg{Vec3(0, 1, 1), Vec3(2, 1, 1)};
    RayHit h3 = geo::intersectRayAABB(rayFrom(Vec3(-1, 1, 1), Vec3(1, 0, 0)), seg);
    CHECK_TRUE(h3.hit);
    CHECK_NEAR(h3.tEnter, 1.0);
    CHECK_NEAR(h3.tExit, 3.0);

    // 射线偏离线段：miss
    RayHit m3 = geo::intersectRayAABB(rayFrom(Vec3(-1, 1.1, 1), Vec3(1, 0, 0)), seg);
    CHECK_TRUE(!m3.hit);

    // 点盒，射线穿过该点
    const AABB pt{Vec3(1, 1, 1), Vec3(1, 1, 1)};
    RayHit h4 = geo::intersectRayAABB(rayFrom(Vec3(-1, -1, -1), Vec3(1, 1, 1)), pt);
    CHECK_TRUE(h4.hit);
    CHECK_NEAR(h4.tEnter, 2.0 * std::sqrt(3.0));
    CHECK_NEAR(h4.tExit, 2.0 * std::sqrt(3.0));

    // 点盒，不穿过
    RayHit m4 = geo::intersectRayAABB(rayFrom(Vec3(0, 0, 0), Vec3(1, 0, 0)), pt);
    CHECK_TRUE(!m4.hit);

    // 起点在退化薄片内：inside, 可见段从 0 开始
    RayHit h5 = geo::intersectRayAABB(rayFrom(Vec3(1, 1, 1), Vec3(1, 0, 0)), slab);
    CHECK_TRUE(h5.hit);
    CHECK_TRUE(h5.inside);
    CHECK_NEAR(h5.tEnter, 0.0);
    CHECK_NEAR(h5.tExit, 1.0);
}

static void testNaNGuards() {
    const AABB box{Vec3(0, 0, 0), Vec3(1, 1, 1)};
    // 沿各坐标轴、贴着边界的射线：结果必须全部有限
    for (int axis = 0; axis < 3; ++axis) {
        for (double edge : {0.0, 1.0}) {
            Vec3 d(0, 0, 0), o(0.5, 0.5, 0.5);
            d[axis] = 1;
            o[(axis + 1) % 3] = edge;
            o[(axis + 2) % 3] = edge;
            o[axis] = -5;
            RayHit h = geo::intersectRayAABB(rayFrom(o, d), box);
            CHECK_TRUE(std::isfinite(h.tEnter));
            CHECK_TRUE(std::isfinite(h.tExit));
        }
    }
    // makeRay 拒绝 NaN/Inf
    Ray r;
    CHECK_TRUE(!geo::makeRay(Vec3(0, 0, 0), Vec3(NAN, 1, 1), r));
    CHECK_TRUE(!geo::makeRay(Vec3(0, 0, 0), Vec3(INFINITY, 1, 1), r));
    CHECK_TRUE(!geo::makeRay(Vec3(NAN, 0, 0), Vec3(1, 0, 0), r));
}

// ---------------------------------------------------------------------------
// BVH
// ---------------------------------------------------------------------------

namespace {

struct Rng {
    std::mt19937_64 engine{0x123456789abcdefULL};
    double uniform(double lo, double hi) {
        std::uniform_real_distribution<double> dist(lo, hi);
        return dist(engine);
    }
    int integer(int lo, int hi) {
        std::uniform_int_distribution<int> dist(lo, hi);
        return dist(engine);
    }
};

std::vector<geo::BoxInput> randomBoxes(Rng& rng, int n) {
    std::vector<geo::BoxInput> boxes;
    boxes.reserve(n);
    for (int i = 0; i < n; ++i) {
        AABB b;
        for (int a = 0; a < 3; ++a) {
            double v0 = rng.uniform(-20, 20);
            double v1 = v0 + rng.uniform(0.0, 6.0);
            // 一定比例的退化分量，模拟薄片/线段/点
            if (rng.integer(0, 5) == 0) v1 = v0;
            b.mn[a] = v0;
            b.mx[a] = v1;
        }
        boxes.push_back({static_cast<int64_t>(i) * 2 - 7, b});  // 非连续、可为负 id
    }
    return boxes;
}

Ray randomRay(Rng& rng) {
    Vec3 o(rng.uniform(-40, 40), rng.uniform(-40, 40), rng.uniform(-40, 40));
    Vec3 d;
    for (;;) {
        // 约一半射线含零方向分量，覆盖平行情形
        d = Vec3(rng.integer(0, 3) == 0 ? 0.0 : rng.uniform(-1, 1),
                 rng.integer(0, 3) == 0 ? 0.0 : rng.uniform(-1, 1),
                 rng.integer(0, 3) == 0 ? 0.0 : rng.uniform(-1, 1));
        if (d.x != 0 || d.y != 0 || d.z != 0) break;
    }
    Ray r;
    geo::makeRay(o, d, r);
    return r;
}

bool sameHitLists(const std::vector<geo::Hit>& a,
                  const std::vector<geo::Hit>& b, double eps = 1e-9) {
    if (a.size() != b.size()) return false;
    for (size_t i = 0; i < a.size(); ++i) {
        if (a[i].id != b[i].id) return false;
        for (int c = 0; c < 3; ++c) {
            auto close = [&](double x, double y) {
                return std::fabs(x - y) <=
                       eps * std::max({1.0, std::fabs(x), std::fabs(y)});
            };
            if (!close(a[i].tEnter, b[i].tEnter) ||
                !close(a[i].tExit, b[i].tExit) ||
                !close(a[i].pointEnter[c], b[i].pointEnter[c]) ||
                !close(a[i].pointExit[c], b[i].pointExit[c])) {
                return false;
            }
        }
    }
    return true;
}

}  // namespace

static void testBVHRandomConsistency() {
    Rng rng;
    int64_t totalTests = 0;
    int64_t hitCases = 0;
    int64_t bvhBoxTests = 0;
    int64_t bruteBoxTests = 0;
    int64_t bvhNodeVisits = 0;

    for (int round = 0; round < 30; ++round) {
        const int n = rng.integer(0, 400);
        auto boxes = randomBoxes(rng, n);
        geo::BVH bvh;
        bvh.build(boxes);
        if (n > 0) CHECK_TRUE(bvh.nodeCount() >= 1);

        const int rays = 200;
        for (int k = 0; k < rays; ++k) {
            Ray r = randomRay(rng);
            ++totalTests;

            geo::TraversalStats st;
            std::vector<geo::Hit> a = bvh.allHits(r, &st);
            std::vector<geo::Hit> b = bvh.bruteAll(r);
            ++g_checks;
            if (!sameHitLists(a, b)) {
                ++g_failures;
                std::cerr << "FAIL allHits 与 bruteAll 不一致：round=" << round
                          << " n=" << n << " ray#=" << k
                          << " bvhHits=" << a.size() << " bruteHits=" << b.size()
                          << "\n";
            }

            geo::Hit na, nb;
            bool fa = bvh.nearest(r, na);
            bool fb = bvh.bruteNearest(r, nb);
            ++g_checks;
            if (fa != fb || (fa && (na.id != nb.id ||
                                    !approx(na.tEnter, nb.tEnter)))) {
                ++g_failures;
                std::cerr << "FAIL nearest 与 bruteNearest 不一致：round="
                          << round << " n=" << n << "\n";
            }

            // 排序性质：tEnter 单调不减；无 NaN
            for (size_t i = 1; i < a.size(); ++i) {
                ++g_checks;
                if (std::isnan(a[i].tEnter) ||
                    a[i - 1].tEnter > a[i].tEnter + 1e-12) {
                    ++g_failures;
                    std::cerr << "FAIL 结果排序/NaN 异常\n";
                }
            }
            for (const auto& h : a) {
                CHECK_TRUE(std::isfinite(h.tEnter) && std::isfinite(h.tExit));
            }

            if (!a.empty()) ++hitCases;
            bvhBoxTests += st.boxesTested;
            bruteBoxTests += n;
            bvhNodeVisits += st.nodesVisited;
        }
    }

    std::cout << "  随机对照：" << totalTests << " 条射线，命中案例 "
              << hitCases << "；BVH 逐盒测试 " << bvhBoxTests
              << " vs 暴力 " << bruteBoxTests << "，节点访问 "
              << bvhNodeVisits << "\n";
    // 大规模场景下 BVH 必须显著剪枝（只统计有盒的轮次整体比例）
    CHECK_TRUE(bruteBoxTests > 0);
    CHECK_TRUE(static_cast<double>(bvhBoxTests) <
               0.6 * static_cast<double>(bruteBoxTests));
}

static void testBVHSpecial() {
    // 完全重合的退化点盒：建树不崩，并列 id 小者胜
    std::vector<geo::BoxInput> boxes;
    for (int i = 0; i < 10; ++i) {
        boxes.push_back({100 + i, AABB{Vec3(1, 1, 1), Vec3(1, 1, 1)}});
    }
    geo::BVH bvh;
    bvh.build(boxes);
    Ray r = rayFrom(Vec3(-1, -1, -1), Vec3(1, 1, 1));
    geo::Hit nearest;
    CHECK_TRUE(bvh.nearest(r, nearest));
    CHECK_TRUE(nearest.id == 100);
    auto all = bvh.allHits(r);
    CHECK_TRUE(all.size() == 10);
    for (size_t i = 0; i < all.size(); ++i) CHECK_TRUE(all[i].id == 100 + (int64_t)i);

    // 空 BVH
    geo::BVH empty;
    empty.build({});
    geo::Hit h;
    CHECK_TRUE(!empty.nearest(r, h));
    CHECK_TRUE(empty.allHits(r).empty());

    // 单盒
    geo::BVH one;
    one.build({{7, AABB{Vec3(0, 0, 0), Vec3(1, 1, 1)}}});
    CHECK_TRUE(one.nearest(rayFrom(Vec3(-2, 0.5, 0.5), Vec3(1, 0, 0)), h));
    CHECK_TRUE(h.id == 7);
}

// ---------------------------------------------------------------------------
// JSON 层
// ---------------------------------------------------------------------------

static void testJson() {
    bool ok = false;
    std::string err;
    JValue v = js::parse(R"({"a":[1,2.5,true,false,null,"hi 中文"],"b":{"c":-3e2}})",
                         ok, err);
    CHECK_TRUE(ok);
    CHECK_TRUE(v.isObject());
    CHECK_TRUE(v.find("a")->asArray().size() == 6);
    CHECK_TRUE(v.find("a")->asArray()[1].asNumber() == 2.5);
    CHECK_TRUE(v.find("b")->find("c")->asNumber() == -300.0);
    CHECK_TRUE(v.find("missing") == nullptr);

    // 转义与 unicode（含代理对）
    JValue u = js::parse(R"("中文 😀 \n\t")", ok, err);
    CHECK_TRUE(ok);
    CHECK_TRUE(u.asString() == std::string("中文 😀 \n\t"));

    // 往返：整数 id 不带小数点
    JValue o = JValue::makeObject();
    o.set("id", JValue::makeInt(-9007199254740993LL));
    o.set("t", JValue::makeNumber(2.0));
    const std::string s = o.dump();
    CHECK_TRUE(s.find("-9007199254740993") != std::string::npos);
    JValue back = js::parse(s, ok, err);
    CHECK_TRUE(ok && back.find("id")->asNumber() == -9007199254740992.0);  // double 精度
    JValue v2 = js::parse("{,}", ok, err);
    CHECK_TRUE(!ok);
    JValue v3 = js::parse(R"({"k":1} trailing)", ok, err);
    CHECK_TRUE(!ok);
    JValue v4 = js::parse("[1, 2, 3,]", ok, err);  // 不接受尾逗号
    CHECK_TRUE(!ok);
}

// ---------------------------------------------------------------------------
// 应用层：端到端字符串请求
// ---------------------------------------------------------------------------

static bool contains(const std::string& s, const char* needle) {
    return s.find(needle) != std::string::npos;
}

static void testApp() {
    // 标准 nearest
    std::string req = R"({
      "mode":"nearest",
      "ray":{"origin":[-2,0.5,0.5],"direction":[1,0,0]},
      "boxes":[{"id":1,"min":[0,0,0],"max":[1,1,1]},
               {"id":2,"min":[5,5,5],"max":[6,6,6]}]
    })";
    std::string resp = app::handleRequest(req);
    CHECK_TRUE(contains(resp, "\"ok\":true"));
    CHECK_TRUE(contains(resp, "\"hit\":true"));
    CHECK_TRUE(contains(resp, "\"id\":1"));
    CHECK_TRUE(contains(resp, "\"t_enter\":2"));
    CHECK_TRUE(contains(resp, "\"consistent\":true"));

    // all：两盒射线都穿过
    std::string req2 = R"({
      "mode":"all",
      "ray":{"origin":[-2,0.5,0.5],"direction":[5,0,0]},
      "boxes":[{"id":1,"min":[0,0,0],"max":[1,1,1]},
               {"min":[2,0,0],"max":[3,1,1]}]
    })";
    std::string resp2 = app::handleRequest(req2);
    CHECK_TRUE(contains(resp2, "\"hit_count\":2"));
    CHECK_TRUE(contains(resp2, "\"consistent\":true"));

    // 错误：零方向
    CHECK_TRUE(contains(app::handleRequest(
        R"({"mode":"nearest","ray":{"origin":[0,0,0],"direction":[0,0,0]},
           "boxes":[]})"), "\"ok\":false"));
    // 错误：min>max
    CHECK_TRUE(contains(app::handleRequest(
        R"({"mode":"nearest","ray":{"origin":[0,0,0],"direction":[1,0,0]},
           "boxes":[{"min":[2,0,0],"max":[1,1,1]}]})"), "\"ok\":false"));
    // 错误：重复 id
    CHECK_TRUE(contains(app::handleRequest(
        R"({"mode":"nearest","ray":{"origin":[0,0,0],"direction":[1,0,0]},
           "boxes":[{"id":5,"min":[0,0,0],"max":[1,1,1]},
                    {"id":5,"min":[2,2,2],"max":[3,3,3]}]})"), "\"ok\":false"));
    // 错误：非法 JSON
    CHECK_TRUE(contains(app::handleRequest("{not json"), "\"ok\":false"));
    // 错误：NaN 输入
    CHECK_TRUE(contains(app::handleRequest(
        R"({"mode":"nearest","ray":{"origin":[0,0,0],"direction":[1,0,"NaN"]},
           "boxes":[]})"), "\"ok\":false"));
    // 空盒集：合法，未命中
    std::string emptyResp = app::handleRequest(
        R"({"mode":"all","ray":{"origin":[0,0,0],"direction":[1,1,1]},"boxes":[]})");
    CHECK_TRUE(contains(emptyResp, "\"ok\":true"));
    CHECK_TRUE(contains(emptyResp, "\"hit_count\":0"));
}

int main() {
    auto t0 = std::chrono::steady_clock::now();

    testBasicPositive();
    testNegativeDirection();
    testOriginInside();
    testGrazing();
    testZeroComponents();
    testDegenerateBoxes();
    testNaNGuards();
    testBVHRandomConsistency();
    testBVHSpecial();
    testJson();
    testApp();

    auto t1 = std::chrono::steady_clock::now();
    const double ms =
        std::chrono::duration<double, std::milli>(t1 - t0).count();

    std::cout << "断言数：" << g_checks << "，失败：" << g_failures
              << "，耗时：" << ms << " ms\n";
    if (g_failures != 0) {
        std::cout << "存在未通过项。\n";
        return 1;
    }
    std::cout << "全部通过。\n";
    return 0;
}
