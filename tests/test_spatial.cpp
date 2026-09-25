// test_spatial.cpp — 空间 KD 树自动化测试
//
// 组成：
//   1. 确定性单元测试：并列距离按 id 排序、K 超过总数、重复坐标、共线点、
//      半径边界包含、空树/空结果、负坐标等。
//   2. 随机对照测试：大量随机数据集与随机查询，逐字段对照全扫描参照实现。
//   3. 边界专项：查询点恰好落在分割平面上、半径恰好等于某些点的距离。
//   4. 极大坐标与性能冒烟测试。
#include <algorithm>
#include <chrono>
#include <cmath>
#include <cstdint>
#include <iostream>
#include <random>
#include <string>
#include <vector>

#include "spatial.hpp"

using spatial::Candidate;
using spatial::KdTree;
using spatial::Point;
using spatial::XY;

namespace {

int g_pass = 0;
int g_fail = 0;

void check(bool cond, const std::string& name, const std::string& detail = "") {
    if (cond) {
        ++g_pass;
        std::cout << "[PASS] " << name << "\n";
    } else {
        ++g_fail;
        std::cout << "[FAIL] " << name
                  << (detail.empty() ? "" : "  -> " + detail) << "\n";
    }
}

std::string idsOf(const std::vector<Candidate>& cs) {
    std::string s = "[";
    for (size_t i = 0; i < cs.size(); ++i) {
        if (i) s += ",";
        s += std::to_string(cs[i].id);
    }
    s += "]";
    return s;
}

// 逐字段对照 KD 树结果与全扫描结果。
bool sameHits(const std::vector<Candidate>& a, const std::vector<Candidate>& b,
              std::string& detail) {
    if (a.size() != b.size()) {
        detail = "结果数量不同: " + std::to_string(a.size()) + " vs " +
                 std::to_string(b.size()) + "，kd ids=" + idsOf(a) +
                 " brute ids=" + idsOf(b);
        return false;
    }
    for (size_t i = 0; i < a.size(); ++i) {
        if (a[i].id != b[i].id || a[i].d2 != b[i].d2 ||
            a[i].x != b[i].x || a[i].y != b[i].y) {
            detail = "第 " + std::to_string(i) + " 项不同: kd(id=" +
                     std::to_string(a[i].id) + ",d2=" + std::to_string(a[i].d2) +
                     ") brute(id=" + std::to_string(b[i].id) + ",d2=" +
                     std::to_string(b[i].d2) + ")";
            return false;
        }
    }
    return true;
}

// 校验结果本身满足“距离升序、距离相同 id 升序”。
bool isOrdered(const std::vector<Candidate>& cs) {
    for (size_t i = 1; i < cs.size(); ++i) {
        if (spatial::better(cs[i], cs[i - 1])) return false;
    }
    return true;
}

// ---------------- 确定性测试 ----------------

void testTieBreakingById() {
    // 查询点 (0,0)：点3(4,3) 与点7(3,4) 距离均为 5，点1(6,0) 距离 6。
    // k=2 应返回两个等距点，且 id 小的在前。
    std::vector<Point> pts = {{7, 3, 4}, {3, 4, 3}, {1, 6, 0}};
    KdTree tree(pts);
    auto r = tree.kNearest(XY{0, 0}, 2);
    bool ok = r.size() == 2 && r[0].id == 3 && r[1].id == 7 &&
              r[0].d2 == 25 && r[1].d2 == 25;
    check(ok, "并列距离按 id 升序", idsOf(r));
}

void testTieThree() {
    // 三个点到 (0,0) 距离都为 5，KNN(k=3) 必须按 id 排序。
    std::vector<Point> pts = {{30, 3, 4}, {10, 4, 3}, {20, -3, -4}};
    KdTree tree(pts);
    auto r = tree.kNearest(XY{0, 0}, 3);
    bool ok = r.size() == 3 && r[0].id == 10 && r[1].id == 20 && r[2].id == 30;
    check(ok, "三点并列时整体按 id 排序", idsOf(r));
}

void testDuplicateCoordinates() {
    // 4 个点完全重合在 (1,1)，id 不同。
    std::vector<Point> pts = {{4, 1, 1}, {1, 1, 1}, {3, 1, 1}, {2, 1, 1}};
    KdTree tree(pts);
    auto r = tree.kNearest(XY{1, 1}, 3);
    bool ok = r.size() == 3 && r[0].id == 1 && r[1].id == 2 && r[2].id == 3 &&
              r[0].d2 == 0;
    check(ok, "重复坐标 KNN 按 id 排序且不去重", idsOf(r));

    std::vector<Candidate> rr;
    tree.radiusQuery(XY{1, 1}, 0, rr);
    bool ok2 = rr.size() == 4 && rr[0].id == 1 && rr[3].id == 4;
    check(ok2, "重复坐标半径 0 返回全部重合点", idsOf(rr));
}

void testCollinearHorizontal() {
    // 全部点共线 y=0，查询点也在该线上。
    std::vector<Point> pts;
    for (std::int64_t i = -10; i <= 10; ++i) pts.push_back({i + 100, static_cast<long double>(i), 0});
    KdTree tree(pts);
    auto r = tree.kNearest(XY{2.5L, 0}, 4);
    // 距离: id102(x=2)->.25, id103(x=3)->.25, id101(x=1)->2.25, id104(x=4)->2.25
    bool ok = r.size() == 4 && r[0].id == 102 && r[1].id == 103 &&
              r[2].id == 101 && r[3].id == 104;
    check(ok, "水平共线点 KNN 并列顺序", idsOf(r));

    auto b = spatial::bruteKNN(pts, XY{2.5L, 0}, 4);
    check(r.size() == b.size() && std::equal(r.begin(), r.end(), b.begin(),
          [](const Candidate& x, const Candidate& y) {
              return x.id == y.id && x.d2 == y.d2;
          }), "水平共线点对照全扫描");
}

void testCollinearVertical() {
    std::vector<Point> pts;
    for (std::int64_t i = 0; i < 20; ++i)
        pts.push_back({i, 5, static_cast<long double>(i) * 2});
    KdTree tree(pts);
    XY q{5, 7};
    auto r = tree.kNearest(q, 5);
    auto b = spatial::bruteKNN(pts, q, 5);
    std::string d;
    check(sameHits(r, b, d), "垂直共线点对照全扫描", d);
}

void testKExceedsN() {
    std::vector<Point> pts = {{1, 0, 0}, {2, 1, 1}};
    KdTree tree(pts);
    bool truncated = false;
    auto r = tree.kNearest(XY{0, 0}, 100, &truncated);
    check(r.size() == 2 && truncated && r[0].id == 1 && r[1].id == 2,
          "K 超过总数时返回全部并标记 truncated");
}

void testKZeroAndEmpty() {
    std::vector<Point> pts = {{1, 0, 0}};
    KdTree tree(pts);
    bool truncated = true;
    auto r = tree.kNearest(XY{0, 0}, 0, &truncated);
    check(r.empty() && !truncated, "k=0 返回空且 truncated=false");

    KdTree empty;
    auto r2 = empty.kNearest(XY{0, 0}, 5, &truncated);
    // k(5) > 点数(0)：truncated 为 true，结果为空
    check(r2.empty() && truncated, "空树 KNN 返回空");
    std::vector<Candidate> rr;
    bool ok = empty.radiusQuery(XY{0, 0}, 5, rr) && rr.empty();
    check(ok, "空树半径查询返回空");
}

void testRadiusBoundary() {
    // 点在半径圆周上必须包含；圆外不包含。
    std::vector<Point> pts = {
        {1, 3, 4},    // d=5，恰好边界
        {2, -3, -4}, // d=5，恰好边界
        {3, 3, 4.1L},// d>5
        {4, 0, 0},   // d=0
    };
    KdTree tree(pts);
    std::vector<Candidate> r;
    tree.radiusQuery(XY{0, 0}, 5, r);
    bool ok = r.size() == 3 && r[0].id == 4 && r[1].id == 1 && r[2].id == 2;
    check(ok, "半径查询边界包含且按 (距离,id) 排序", idsOf(r));

    std::vector<Candidate> rNeg;
    check(!tree.radiusQuery(XY{0, 0}, -1, rNeg), "负半径返回非法标志");
}

void testNegativeCoordinates() {
    std::vector<Point> pts = {{1, -10, -10}, {2, 10, 10}, {3, -9, -11}};
    KdTree tree(pts);
    XY q{-10, -10};
    auto r = tree.kNearest(q, 2);
    auto b = spatial::bruteKNN(pts, q, 2);
    std::string d;
    check(sameHits(r, b, d), "负坐标 KNN 对照全扫描", d);
}

void testQueryFarAway() {
    std::vector<Point> pts = {{1, 0, 0}, {2, 1, 0}, {3, 0, 1}};
    KdTree tree(pts);
    XY q{1e6L, -1e6L};
    auto r = tree.kNearest(q, 10);
    auto b = spatial::bruteKNN(pts, q, 10);
    std::string d;
    check(sameHits(r, b, d) && isOrdered(r), "远离点集的查询对照全扫描", d);

    std::vector<Candidate> rr;
    tree.radiusQuery(q, 1, rr);
    check(rr.empty(), "小半径远离点集返回空");
}

void testSinglePoint() {
    std::vector<Point> pts = {{42, -7, 13}};
    KdTree tree(pts);
    auto r = tree.kNearest(XY{-7, 13}, 1);
    check(r.size() == 1 && r[0].id == 42 && r[0].d2 == 0, "单点树查询到自身");
    auto r2 = tree.kNearest(XY{0, 0}, 5);
    check(r2.size() == 1 && r2[0].id == 42, "单点树 k>N 仍返回该点");
}

// ---------------- 随机对照测试 ----------------

struct Rng {
    std::mt19937_64 gen;
    explicit Rng(std::uint64_t seed) : gen(seed) {}
    // [-scale, scale] 上的长双精度，混合大尺度与整数，制造并列/共线机会。
    long double coord(long double scale, int mode) {
        if (mode == 0) {
            // 离散网格点：大量并列距离、重复坐标、共线
            long long v = static_cast<long long>(gen() % 41) - 20;  // -20..20
            return static_cast<long double>(v);
        }
        std::uniform_real_distribution<double> u(-1.0, 1.0);
        return static_cast<long double>(u(gen)) * scale;
    }
    std::size_t idx(std::size_t n) {
        return n == 0 ? 0 : gen() % n;
    }
};

void randomComparison(std::uint64_t seed, std::size_t n, long double scale, int mode,
                      std::size_t trials) {
    Rng rng(seed);
    std::vector<Point> pts;
    pts.reserve(n);
    for (std::size_t i = 0; i < n; ++i) {
        Point p;
        p.id = static_cast<std::int64_t>(i) + 1;
        p.x = rng.coord(scale, mode);
        p.y = (mode == 2) ? 0.0L : rng.coord(scale, mode);  // mode 2: 全部 y=0 共线
        pts.push_back(p);
    }
    KdTree tree(pts);

    int bad = 0;
    std::string firstDetail;
    for (std::size_t t = 0; t < trials; ++t) {
        XY q{rng.coord(scale, mode == 2 ? 0 : mode),
             mode == 2 ? 0.0L : rng.coord(scale, mode)};
        std::size_t k = rng.idx(n + 4);  // 0 .. n+3，覆盖 k>N
        auto r = tree.kNearest(q, k);
        auto b = spatial::bruteKNN(pts, q, k);
        std::string d;
        if (!sameHits(r, b, d) || !isOrdered(r)) {
            ++bad;
            if (firstDetail.empty()) firstDetail = d + " (seed=" + std::to_string(seed) + ")";
        }

        // 半径：0、网格边界值（5、sqrt(2)*整数等）、随机值、极大值
        long double radius;
        int pick = static_cast<int>(rng.idx(5));
        if (pick == 0) radius = 0;
        else if (pick == 1) radius = 5;
        else if (pick == 2) radius = static_cast<long double>(rng.idx(60));
        else if (pick == 3) radius = scale * 10;
        else radius = std::fabs(rng.coord(scale, mode)) + 0.5L;

        std::vector<Candidate> rr;
        tree.radiusQuery(q, radius, rr);
        auto bb = spatial::bruteRadius(pts, q, radius);
        std::string d2;
        if (!sameHits(rr, bb, d2)) {
            ++bad;
            if (firstDetail.empty())
                firstDetail = "radius: " + d2 + " (seed=" + std::to_string(seed) + ")";
        }
    }
    std::string name = "随机对照 n=" + std::to_string(n) + " scale=" +
                       std::to_string(static_cast<double>(scale)) +
                       " mode=" + std::to_string(mode) +
                       " trials=" + std::to_string(trials);
    check(bad == 0, name, firstDetail);
}

void testBoundarySweep() {
    // 构造密集规则网格，查询点取网格交点与偏移 0.5 的位置，半径扫过多个
    // 恰好等于真实点距的值 —— 专门压测“平面距离 == 最差距离”的剪枝边界。
    std::vector<Point> pts;
    std::int64_t id = 1;
    for (int gx = -8; gx <= 8; ++gx)
        for (int gy = -8; gy <= 8; ++gy)
            pts.push_back({id++, static_cast<long double>(gx),
                           static_cast<long double>(gy)});
    KdTree tree(pts);

    int bad = 0;
    std::string first;
    int qxVals[] = {-8, -4, 0, 3, 8};
    for (int qxi = 0; qxi < 5; ++qxi) {
        long double qx = qxVals[qxi] + 0.5L;
        for (int qy = -8; qy <= 8; ++qy) {
            XY q{qx, static_cast<long double>(qy)};
            for (std::size_t k : {1ul, 2ul, 4ul, 9ul, 25ul, 100ul, 1000ul}) {
                auto r = tree.kNearest(q, k);
                auto b = spatial::bruteKNN(pts, q, k);
                std::string d;
                if (!sameHits(r, b, d)) { ++bad; if (first.empty()) first = d; }
            }
            for (long double radius : {0.5L, 1.0L, std::sqrt(2.0L), 2.5L,
                                       5.0L, std::sqrt(50.0L), 50.0L}) {
                std::vector<Candidate> rr, bb;
                tree.radiusQuery(q, radius, rr);
                bb = spatial::bruteRadius(pts, q, radius);
                std::string d;
                if (!sameHits(rr, bb, d)) { ++bad; if (first.empty()) first = d; }
            }
        }
    }
    check(bad == 0, "网格边界扫描（平面/圆周等距压测）", first);
}

void testHugeCoordinates() {
    // 极大坐标：1e150 量级。注意 long double(80 位) 在 1e150 处的 ULP 约 1.08e131，
    // 偏移必须大于 ULP 才能与 B 区分，因此点4使用 1e135 的偏移（而非 1e120）。
    const long double B = 1e150L;
    std::vector<Point> pts = {
        {1, B, 0},
        {2, -B, 0},
        {3, 0, B},
        {4, B + 1e135L, 0},  // 与点1差距大于 ULP，距离严格更大
    };
    KdTree tree(pts);
    XY q{0, 0};
    auto r = tree.kNearest(q, 4);
    auto b = spatial::bruteKNN(pts, q, 4);
    std::string d;
    bool ok = sameHits(r, b, d) && isOrdered(r);
    check(ok, "极大坐标 1e150 对照全扫描", d);
    check(r.size() == 4 && std::isfinite(r[0].d2) && r[0].d2 == B * B,
          "极大坐标平方距离有限且精确");

    std::vector<Candidate> rr;
    tree.radiusQuery(q, B, rr);  // 半径 B：点 1/2/3 恰好距离 B，必须包含
    check(rr.size() == 3 && rr[0].id == 1 && rr[1].id == 2 && rr[2].id == 3,
          "极大坐标半径边界包含", idsOf(rr));
}

void testExtremeFloatNearLimit() {
    // 接近坐标安全上限：平方距离仍有限（1e2400 -> 平方 1e4800 < 1e4932）。
    const long double B = 9e2399L;
    std::vector<Point> pts = {{1, B, 0}, {2, 0, 0}, {3, -B, 0}};
    KdTree tree(pts);
    XY q{0, 0};
    auto r = tree.kNearest(q, 3);
    check(r.size() == 3 && r[0].id == 2 && std::isfinite(r[2].d2),
          "近上限坐标 9e2399 平方距离有限");
    std::vector<Candidate> rr;
    // 点2在原点（查询点）距离0，点1/点3距离恰好 B，共 3 个
    check(tree.radiusQuery(q, B, rr) && rr.size() == 3,
          "近上限坐标半径边界查询");
}

void testPerformanceSmoke() {
    // 性能冒烟：10 万点构建 + 1000 次 KNN，记录耗时（不做硬性阈值断言，
    // 只验证规模可承受；具体时间打印输出）。
    const std::size_t n = 100000;
    std::vector<Point> pts;
    pts.reserve(n);
    std::mt19937_64 gen(0xC0FFEE);
    std::uniform_real_distribution<double> u(-1000, 1000);
    for (std::size_t i = 0; i < n; ++i)
        pts.push_back({static_cast<std::int64_t>(i) + 1,
                       static_cast<long double>(u(gen)),
                       static_cast<long double>(u(gen))});

    auto t0 = std::chrono::steady_clock::now();
    KdTree tree(std::move(pts));
    auto t1 = std::chrono::steady_clock::now();

    volatile std::int64_t sink = 0;
    for (int i = 0; i < 1000; ++i) {
        XY q{static_cast<long double>(u(gen)), static_cast<long double>(u(gen))};
        auto r = tree.kNearest(q, 10);
        if (!r.empty()) sink += r[0].id;
    }
    auto t2 = std::chrono::steady_clock::now();

    auto buildMs = std::chrono::duration_cast<std::chrono::milliseconds>(t1 - t0).count();
    auto queryMs = std::chrono::duration_cast<std::chrono::milliseconds>(t2 - t1).count();
    std::cout << "  [INFO] 构建 " << n << " 点: " << buildMs << " ms；"
              << "1000 次 k=10 查询: " << queryMs << " ms (sink=" << sink << ")\n";
    check(tree.size() == n, "10 万点规模冒烟（耗时见 INFO）");
}

void testDepthBalanced() {
    // 全部坐标相同：树仍应平衡（nth_element 按 id 次序完成划分）。
    const int n = 100000;
    std::vector<Point> pts;
    for (int i = 0; i < n; ++i) pts.push_back({i, 7, 7});
    KdTree tree(std::move(pts));
    auto r = tree.kNearest(XY{7, 7}, 10);
    bool ok = r.size() == 10;
    for (int i = 0; i < 10; ++i) ok = ok && (r[i].id == i);
    check(ok, "全部坐标相同：10 万点 KNN 正确（树平衡、无栈溢出）", idsOf(r));
}

}  // namespace

int main() {
    std::cout << "==== 确定性单元测试 ====\n";
    testTieBreakingById();
    testTieThree();
    testDuplicateCoordinates();
    testCollinearHorizontal();
    testCollinearVertical();
    testKExceedsN();
    testKZeroAndEmpty();
    testRadiusBoundary();
    testNegativeCoordinates();
    testQueryFarAway();
    testSinglePoint();

    std::cout << "\n==== 边界专项 ====\n";
    testBoundarySweep();

    std::cout << "\n==== 随机对照全扫描 ====\n";
    randomComparison(1001, 1, 100, 1, 10);          // 单点
    randomComparison(1002, 2, 100, 1, 20);          // 两点
    randomComparison(1003, 50, 100, 1, 300);        // 连续随机
    randomComparison(1004, 200, 1e6, 1, 300);       // 大尺度连续随机
    randomComparison(1005, 300, 1, 0, 300);         // 离散网格（大量并列/重复）
    randomComparison(1006, 300, 1, 2, 200);         // 全共线 y=0
    randomComparison(1007, 1000, 10, 0, 200);       // 网格大点集
    randomComparison(1008, 777, 1e100, 1, 200);     // 超大尺度随机
    randomComparison(1009, 1234, 1e150, 1, 200);    // 极大坐标随机
    randomComparison(1010, 5000, 1000, 1, 100);     // 中等规模

    std::cout << "\n==== 极大坐标 ====\n";
    testHugeCoordinates();
    testExtremeFloatNearLimit();

    std::cout << "\n==== 性能与鲁棒性冒烟 ====\n";
    testDepthBalanced();
    testPerformanceSmoke();

    std::cout << "\n=====================================\n";
    std::cout << "通过: " << g_pass << "，失败: " << g_fail << "\n";
    return g_fail == 0 ? 0 : 1;
}
