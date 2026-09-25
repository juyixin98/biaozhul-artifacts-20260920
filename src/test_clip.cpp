// test_clip.cpp - 直接针对 geometry.hpp 的确定性单元测试（含手算案例）
// 编译: g++ -std=c++17 -O2 -Wall -Wextra -I src src/test_clip.cpp -o build/test_clip
#include <algorithm>
#include <cmath>
#include <cstdio>
#include <string>
#include <vector>

#include "geometry.hpp"

using geom::Point;
using geom::Ring;
using geom::Result;
using geom::ResultKind;
using geom::StatusCode;

static int g_fail = 0;
static int g_checks = 0;

#define CHECK(cond)                                                            \
    do {                                                                       \
        ++g_checks;                                                            \
        if (!(cond)) {                                                         \
            ++g_fail;                                                          \
            std::printf("  [FAIL] %s  (%s:%d)\n", #cond, __FILE__, __LINE__);  \
        }                                                                      \
    } while (0)

static bool near(double a, double b, double eps = 1e-8) {
    return std::abs(a - b) <= eps * std::max(1.0, std::max(std::abs(a), std::abs(b)));
}
static bool pnear(const Point& a, const Point& b, double eps = 1e-8) {
    return near(a.x, b.x, eps) && near(a.y, b.y, eps);
}
static bool has_point(const Ring& r, const Point& p, double eps = 1e-8) {
    for (const auto& q : r)
        if (pnear(q, p, eps)) return true;
    return false;
}
static Ring rect(double x0, double y0, double x1, double y1) {
    // CCW
    return {{x0, y0}, {x1, y0}, {x1, y1}, {x0, y1}};
}
// 所有结果点是否在裁剪闭区域内
static bool all_inside(const Ring& r, const Ring& clip_ccw, double tol) {
    for (const auto& p : r)
        if (!geom::point_in_convex(p, clip_ccw, tol)) return false;
    return true;
}

static void test_hand_computed_triangle() {
    std::printf("[test] 手算案例：大三角形被单位格矩形裁剪\n");
    // 裁剪域: [0,10] x [0,10]
    Ring clip = rect(0, 0, 10, 10);
    // 被裁剪三角形 (-5,5)->(5,-5)->(15,5)，面积 100
    Ring subj = {{-5, 5}, {5, -5}, {15, 5}};
    CHECK(near(geom::signed_area(subj), 100.0));

    Result r = geom::clip_polygon(subj, clip);
    CHECK(r.status == StatusCode::OK);
    CHECK(r.kind == ResultKind::POLYGON);
    // 手算结果: 矩形 (0,0)(10,0)(10,5)(0,5)，面积 50
    CHECK(r.ring.size() == 4);
    CHECK(near(r.area, 50.0));
    CHECK(r.signed_area > 0); // 统一 CCW
    CHECK(has_point(r.ring, {0, 0}));
    CHECK(has_point(r.ring, {10, 0}));
    CHECK(has_point(r.ring, {10, 5}));
    CHECK(has_point(r.ring, {0, 5}));
    CHECK(all_inside(r.ring, clip, r.tol));
}

static void test_fully_contained() {
    std::printf("[test] 全包含：小矩形完全在裁剪矩形内\n");
    Ring clip = rect(0, 0, 10, 10);
    Ring subj = rect(2, 2, 8, 8);
    Result r = geom::clip_polygon(subj, clip);
    CHECK(r.status == StatusCode::OK);
    CHECK(r.kind == ResultKind::POLYGON);
    CHECK(r.ring.size() == 4);
    CHECK(near(r.area, 36.0));
    CHECK(all_inside(r.ring, clip, r.tol));
}

static void test_cw_input_normalized() {
    std::printf("[test] 顺时针输入 -> 输出统一为 CCW\n");
    Ring clip = rect(0, 0, 10, 10);
    Ring cw = {{2, 2}, {2, 8}, {8, 8}, {8, 2}}; // 顺时针
    CHECK(geom::signed_area(cw) < 0);
    Result r = geom::clip_polygon(cw, clip);
    CHECK(r.kind == ResultKind::POLYGON);
    CHECK(r.signed_area > 0);
    CHECK(near(r.area, 36.0));
}

static void test_no_intersection() {
    std::printf("[test] 无交集\n");
    Ring clip = rect(0, 0, 10, 10);
    Ring subj = rect(-10, -10, -9, -9);
    Result r = geom::clip_polygon(subj, clip);
    CHECK(r.status == StatusCode::OK);
    CHECK(r.kind == ResultKind::EMPTY);
    CHECK(r.ring.empty());
    CHECK(near(r.area, 0.0));
}

static void test_edge_coincidence() {
    std::printf("[test] 沿边重合：底边与裁剪边 y=0 重合\n");
    Ring clip = rect(0, 0, 10, 10);
    // 被裁剪矩形跨越 y=0，其内边沿 [2,0]-[8,0] 与裁剪边重合
    Ring subj = {{2, -2}, {8, -2}, {8, 2}, {2, 2}};
    Result r = geom::clip_polygon(subj, clip);
    CHECK(r.status == StatusCode::OK);
    CHECK(r.kind == ResultKind::POLYGON);
    // 手算结果: (2,0)(8,0)(8,2)(2,2) 面积 12
    CHECK(r.ring.size() == 4);
    CHECK(near(r.area, 12.0));
    CHECK(has_point(r.ring, {2, 0}));
    CHECK(has_point(r.ring, {8, 0}));
    CHECK(has_point(r.ring, {8, 2}));
    CHECK(has_point(r.ring, {2, 2}));
    CHECK(all_inside(r.ring, clip, r.tol));
}

static void test_degenerate_segment() {
    std::printf("[test] 退化：交集仅为一条边段（面积为 0）\n");
    Ring clip = rect(0, 0, 10, 10);
    // 横条 y∈[-2,0]，与裁剪域仅在 y=0 的 [0,10] 段接触
    Ring subj = {{-2, -2}, {12, -2}, {12, 0}, {-2, 0}};
    Result r = geom::clip_polygon(subj, clip);
    CHECK(r.status == StatusCode::OK);
    CHECK(r.kind == ResultKind::SEGMENT);
    CHECK(r.ring.size() == 2);
    CHECK(near(r.area, 0.0));
    CHECK(has_point(r.ring, {0, 0}));
    CHECK(has_point(r.ring, {10, 0}));
    CHECK(all_inside(r.ring, clip, r.tol));
}

static void test_degenerate_point() {
    std::printf("[test] 退化：仅角点接触\n");
    Ring clip = rect(0, 0, 10, 10);
    // 三角形只以顶点 (0,0) 触及裁剪域角点
    Ring subj = {{-5, -5}, {5, -5}, {0, 0}};
    Result r = geom::clip_polygon(subj, clip);
    CHECK(r.status == StatusCode::OK);
    CHECK(r.kind == ResultKind::POINT);
    CHECK(r.ring.size() == 1);
    CHECK(pnear(r.ring[0], {0, 0}, 1e-9));
    CHECK(near(r.area, 0.0));
}

static void test_self_intersection_rejected() {
    std::printf("[test] 自交（蝴蝶结）必须被拒绝\n");
    Ring clip = rect(0, 0, 10, 10);
    Ring bow = {{0, 0}, {10, 10}, {0, 10}, {10, 0}};
    Result r = geom::clip_polygon(bow, clip);
    CHECK(r.status == StatusCode::SELF_INTERSECTING);
    CHECK(r.ring.empty());
}

static void test_self_touch_rejected() {
    std::printf("[test] 自触多边形（非相邻顶点接触）必须被拒绝\n");
    Ring clip = rect(-20, -20, 20, 20);
    // 两个三角形在点 (0,5) 处捏合（pinched）
    Ring pinch = {{-10, 0}, {0, 5}, {-10, 10}, {10, 10}, {0, 5}, {10, 0}};
    Result r = geom::clip_polygon(pinch, clip);
    CHECK(r.status == StatusCode::SELF_INTERSECTING);
}

static void test_nonconvex_clip_rejected() {
    std::printf("[test] 非凸/带共线点的裁剪多边形被拒绝\n");
    Ring subj = rect(1, 1, 9, 9);
    Ring concave = {{0, 0}, {10, 0}, {10, 10}, {5, 5}, {0, 10}};
    Result r = geom::clip_polygon(subj, concave);
    CHECK(r.status == StatusCode::INVALID_CLIP);

    Ring collinear = {{0, 0}, {5, 0}, {10, 0}, {10, 10}, {0, 10}};
    Result r2 = geom::clip_polygon(subj, collinear);
    CHECK(r2.status == StatusCode::INVALID_CLIP);

    Ring too_few = {{0, 0}, {1, 0}};
    Result r3 = geom::clip_polygon(subj, too_few);
    CHECK(r3.status == StatusCode::INVALID_CLIP);
}

static void test_area_bounds_random(double (*rng)(), int seed_note) {
    (void)seed_note;
    // 随机星形简单多边形被随机凸多边形裁剪：面积界与包含性
    const int N = 200;
    for (int it = 0; it < N; ++it) {
        // 随机凸裁剪域：单位圆上随机点按角度排序
        int m = 3 + (int)(rng() * 5);
        std::vector<std::pair<double, double>> pts;
        for (int i = 0; i < m; ++i) {
            double a = rng() * 2.0 * M_PI;
            double rr = 5.0 + rng() * 5.0;
            double cx = (rng() - 0.5) * 6.0;
            double cy = (rng() - 0.5) * 6.0;
            pts.push_back({cx + rr * std::cos(a), cy + rr * std::sin(a)});
        }
        std::sort(pts.begin(), pts.end());
        // 凸包（Andrew monotone chain）保证严格凸
        std::vector<Point> hull;
        auto cross3 = [](Point O, Point A, Point B) {
            return (A.x - O.x) * (B.y - O.y) - (A.y - O.y) * (B.x - O.x);
        };
        std::vector<Point> lower, upper;
        std::vector<Point> ppts;
        for (auto& q : pts) ppts.push_back({q.first, q.second});
        for (auto& p : ppts) {
            while (lower.size() >= 2 &&
                   cross3(lower[lower.size() - 2], lower.back(), p) <= 0)
                lower.pop_back();
            lower.push_back(p);
        }
        for (auto it2 = ppts.rbegin(); it2 != ppts.rend(); ++it2) {
            while (upper.size() >= 2 &&
                   cross3(upper[upper.size() - 2], upper.back(), *it2) <= 0)
                upper.pop_back();
            upper.push_back(*it2);
        }
        lower.pop_back();
        upper.pop_back();
        hull = lower;
        for (auto& p : upper) hull.push_back(p);
        if (hull.size() < 3) continue;

        // 随机星形简单多边形（半径正、绕中心角序 => 必简单）
        double cx = (rng() - 0.5) * 10.0;
        double cy = (rng() - 0.5) * 10.0;
        int k = 3 + (int)(rng() * 6);
        Ring subj;
        for (int i = 0; i < k; ++i) {
            double a = 2.0 * M_PI * i / k;
            double rr = 1.0 + rng() * 8.0;
            subj.push_back({cx + rr * std::cos(a), cy + rr * std::sin(a)});
        }

        Result r = geom::clip_polygon(subj, hull);
        if (r.status != StatusCode::OK) {
            std::printf("  [FAIL] 随机用例被拒（iter=%d）\n", it);
            ++g_fail;
            continue;
        }
        double a_subj = std::abs(geom::signed_area(subj));
        double a_clip = std::abs(geom::signed_area(hull));
        if (r.kind == ResultKind::POLYGON) {
            // 面积界: 0 <= 结果面积 <= min(两者面积)
            CHECK(r.area >= -1e-7);
            CHECK(r.area <= a_subj + 1e-6 * std::max(1.0, a_subj));
            CHECK(r.area <= a_clip + 1e-6 * std::max(1.0, a_clip));
            CHECK(r.signed_area > 0);
            CHECK(all_inside(r.ring, hull, r.tol));
        } else {
            CHECK(near(r.area, 0.0));
            CHECK(all_inside(r.ring, hull, std::max(r.tol, 1e-9)));
        }
    }
}

// 简单确定性 LCG，避免平台 rand 差异
static unsigned long g_state = 12345;
static double lcg() {
    g_state = g_state * 6364136223846793005UL + 1442695040888963407UL;
    return (double)(g_state >> 11) / (double)(1UL << 53);
}

int main() {
    test_hand_computed_triangle();
    test_fully_contained();
    test_cw_input_normalized();
    test_no_intersection();
    test_edge_coincidence();
    test_degenerate_segment();
    test_degenerate_point();
    test_self_intersection_rejected();
    test_self_touch_rejected();
    test_nonconvex_clip_rejected();
    std::printf("[test] 随机不变量（面积界 + 结果点在裁剪域内）x200\n");
    test_area_bounds_random(lcg, 0);

    std::printf("\n==== %d 项断言，%d 项失败 ====\n", g_checks, g_fail);
    return g_fail == 0 ? 0 : 1;
}
