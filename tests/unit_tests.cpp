// unit_tests.cpp - 几何核心的 C++ 单元测试
#include <cmath>
#include <cstdio>
#include <string>
#include <vector>

#include "geometry.hpp"

using geom::Point;

static int g_failures = 0;
static int g_checks = 0;

#define CHECK(cond)                                                     \
    do {                                                                \
        ++g_checks;                                                     \
        if (!(cond)) {                                                  \
            ++g_failures;                                               \
            std::printf("FAIL %s:%d  %s\n", __FILE__, __LINE__, #cond); \
        }                                                               \
    } while (0)

static bool approx(long double a, long double b, long double tol = 1e-9L) {
    return fabsl(a - b) <= tol;
}

static bool samePt(const Point& a, long double x, long double y,
                   long double tol = 1e-9L) {
    return approx(a.x, x, tol) && approx(a.y, y, tol);
}

static std::vector<Point> ring(std::initializer_list<std::pair<double, double>> pts) {
    std::vector<Point> v;
    for (auto [x, y] : pts) v.push_back({x, y});
    return v;
}

// 面积恒等式: 0 <= area(subj ∩ clip) <= min(area(subj), area(clip))。
static void checkAreaBounds(const std::vector<Point>& s,
                            const std::vector<Point>& c,
                            long double tol = 1e-7L) {
    geom::ClipResult r;
    std::string err = geom::clipPolygonByConvex(s, c, geom::Eps{}, r);
    long double as = fabsl(geom::signedArea(s));
    long double ac = fabsl(geom::signedArea(c));
    if (!err.empty()) {
        std::printf("  (clip rejected: %s)\n", err.c_str());
        return;
    }
    CHECK(r.area >= -tol);
    CHECK(r.area <= as + tol);
    CHECK(r.area <= ac + tol);
    if (r.kind == geom::ResultKind::Polygon) {
        CHECK(r.area > 0);
        // 每个结果点必须落在主体与裁剪域内(含边界)。
        for (const auto& p : r.vertices) {
            CHECK(geom::pointInPolygon(p, s, geom::Eps{}));
            CHECK(geom::pointInConvexCCW(p, c, geom::Eps{}));
        }
    }
}

int main() {
    const geom::Eps eps{};

    // ---- 1. 矩形手算案例(三角形被矩形裁剪, 梯形结果) ----
    {
        // clip: [0,10]x[0,10]  CCW
        auto clip = ring({{0, 0}, {10, 0}, {10, 10}, {0, 10}});
        // subject: 三角形 (2,-2) (18,4) (6,16) CCW
        auto subj = ring({{2, -2}, {18, 4}, {6, 16}});
        geom::ClipResult r;
        std::string err = geom::clipPolygonByConvex(subj, clip, eps, r);
        CHECK(err.empty());
        CHECK(r.kind == geom::ResultKind::Polygon);
        CHECK(r.vertices.size() == 5);
        // 手算交点(逐裁剪边):
        // (2,-2)->(18,4): y=0 处 t=1/3 -> (22/3, 0); x=10 处 t=1/2 -> (10, 1)
        // 边 (18,4)->(6,16) 所在直线 x+y=22, 经过 (11,11);
        // 矩形角点 (10,10) 满足 x+y=20<22, 严格位于三角形内, 故结果含 (10,10)
        // (6,16)->(2,-2): y=10 处 t=3/8 -> (14/3, 10); y=0 处 t=8/9 -> (22/9, 0)
        bool foundA = false, foundB = false, foundC = false, foundD = false;
        bool foundE = false;
        for (const auto& p : r.vertices) {
            if (samePt(p, 22.0L / 3, 0, 1e-8)) foundA = true;
            if (samePt(p, 10, 1, 1e-8)) foundB = true;
            if (samePt(p, 10, 10, 1e-8)) foundC = true;
            if (samePt(p, 14.0L / 3, 10, 1e-8)) foundD = true;
            if (samePt(p, 22.0L / 9, 0, 1e-8)) foundE = true;
        }
        CHECK(foundA);
        CHECK(foundB);
        CHECK(foundC);
        CHECK(foundD);
        CHECK(foundE);
        // 五边形 (22/9,0),(22/3,0),(10,1),(10,10),(14/3,10) 鞋带面积 = 568/9
        CHECK(approx(r.area, 568.0L / 9, 1e-8));
        CHECK(r.orientation == 1);
        CHECK(geom::signedArea(r.vertices) > 0);  // CCW
        checkAreaBounds(subj, clip);
    }

    // ---- 2. 全包含: 主体完全在裁剪域内 ----
    {
        auto clip = ring({{0, 0}, {10, 0}, {10, 10}, {0, 10}});
        auto subj = ring({{1, 1}, {3, 1}, {3, 3}, {1, 3}});
        geom::ClipResult r;
        CHECK(geom::clipPolygonByConvex(subj, clip, eps, r).empty());
        CHECK(r.kind == geom::ResultKind::Polygon);
        CHECK(r.vertices.size() == 4);
        CHECK(approx(r.area, 4.0));
        for (const auto& p : r.vertices)
            CHECK(geom::pointInConvexCCW(p, clip, eps));
    }

    // ---- 3. 无交: 分离 ----
    {
        auto clip = ring({{0, 0}, {10, 0}, {10, 10}, {0, 10}});
        auto subj = ring({{20, 20}, {30, 20}, {30, 30}, {20, 30}});
        geom::ClipResult r;
        CHECK(geom::clipPolygonByConvex(subj, clip, eps, r).empty());
        CHECK(r.kind == geom::ResultKind::Empty);
        CHECK(r.vertices.empty());
        CHECK(r.area == 0);
    }

    // ---- 4. 沿边重合(有面积): 主体一条边与裁剪边共线 ----
    {
        auto clip = ring({{0, 0}, {10, 0}, {10, 10}, {0, 10}});
        auto subj = ring({{2, 0}, {8, 0}, {8, 6}, {2, 6}});
        geom::ClipResult r;
        CHECK(geom::clipPolygonByConvex(subj, clip, eps, r).empty());
        CHECK(r.kind == geom::ResultKind::Polygon);
        CHECK(r.vertices.size() == 4);
        CHECK(approx(r.area, 36.0));
    }

    // ---- 5. 沿边重合(零面积): 主体退化为裁剪边界上的线段 ----
    {
        auto clip = ring({{0, 0}, {10, 0}, {10, 10}, {0, 10}});
        // 极薄三角形: 全部点都在 y=0 上的区间 (2,0)-(8,0) 附近, 面积为 0
        auto subj = ring({{2, 0}, {8, 0}, {5, 0}});
        geom::ClipResult r;
        std::string err = geom::clipPolygonByConvex(subj, clip, eps, r);
        // 输入三角形三点共线, 主体校验会拒绝为退化多边形
        CHECK(!err.empty());
    }

    // ---- 6. 点接触: 主体仅一个顶点接触裁剪顶点, 无面积 ----
    {
        auto clip = ring({{0, 0}, {10, 0}, {10, 10}, {0, 10}});
        auto subj = ring({{0, 10}, {-10, 20}, {-5, 12}});
        geom::ClipResult r;
        std::string err = geom::clipPolygonByConvex(subj, clip, eps, r);
        CHECK(err.empty());
        CHECK((r.kind == geom::ResultKind::Point));
        if (r.kind == geom::ResultKind::Point) {
            CHECK(r.vertices.size() == 1);
            CHECK(samePt(r.vertices[0], 0, 10));
        }
    }

    // ---- 7. 沿边重合为线段: 三角形只有底边在裁剪边上, 其余在外 ----
    {
        auto clip = ring({{0, 0}, {10, 0}, {10, 10}, {0, 10}});
        // 主体三角形在裁剪域外(下侧), 仅顶边 (2,0)-(8,0) 与裁剪底边接触
        auto subj = ring({{2, 0}, {8, 0}, {5, -6}});
        geom::ClipResult r;
        std::string err = geom::clipPolygonByConvex(subj, clip, eps, r);
        CHECK(err.empty());
        CHECK(r.kind == geom::ResultKind::Segment);
        CHECK(r.vertices.size() == 2);
    }

    // ---- 8. 非凸裁剪多边形被拒绝 ----
    {
        auto clip = ring({{0, 0}, {10, 0}, {5, 5}, {10, 10}, {0, 10}});
        auto subj = ring({{1, 1}, {9, 1}, {9, 9}, {1, 9}});
        geom::ClipResult r;
        std::string err = geom::clipPolygonByConvex(subj, clip, eps, r);
        CHECK(err.find("INVALID_CLIP") != std::string::npos);
    }

    // ---- 9. 自交主体(蝴蝶结)被拒绝 ----
    {
        auto clip = ring({{0, 0}, {10, 0}, {10, 10}, {0, 10}});
        auto bow = ring({{1, 1}, {9, 9}, {9, 1}, {1, 9}});
        geom::ClipResult r;
        std::string err = geom::clipPolygonByConvex(bow, clip, eps, r);
        CHECK(err.find("INVALID_SUBJECT") != std::string::npos);
        CHECK(err.find("self-intersect") != std::string::npos);
    }

    // ---- 10. CW 输入: 输出仍统一为 CCW ----
    {
        auto clip = ring({{0, 0}, {10, 0}, {10, 10}, {0, 10}});
        auto subjCW = ring({{1, 1}, {1, 3}, {3, 3}, {3, 1}});
        CHECK(geom::signedArea(subjCW) < 0);
        geom::ClipResult r;
        CHECK(geom::clipPolygonByConvex(subjCW, clip, eps, r).empty());
        CHECK(r.kind == geom::ResultKind::Polygon);
        CHECK(r.orientation == 1);
        CHECK(geom::signedArea(r.vertices) > 0);
        CHECK(r.inputSubjectReversed);
        CHECK(approx(r.area, 4.0));
    }

    // ---- 11. 凹主体合法但交为两个分离分支: 明确报错而非静默 ----
    // U 形主体被水平条 y∈[6,9] 截取时形成两个分离矩形(臂);
    // Sutherland-Hodgman 单环输出会沿裁剪边回折, 此处必须明确拒绝。
    // 注意: 条带取 y∈[2,5] 时交集在 y∈[2,4] 是连通的(合法凹多边形)。
    {
        auto clip = ring({{0, 6}, {10, 6}, {10, 9}, {0, 9}});
        auto u = ring({{0, 0}, {10, 0}, {10, 10}, {6, 10},
                       {6, 4}, {4, 4}, {4, 10}, {0, 10}});
        geom::ClipResult r;
        std::string err = geom::clipPolygonByConvex(u, clip, eps, r);
        CHECK(!err.empty());
        CHECK(err.find("MULTIPLE_COMPONENTS") != std::string::npos ||
              err.find("NOT_SIMPLE") != std::string::npos);
    }

    // ---- 12. 退化输入: 尖刺回折被拒绝 ----
    {
        auto clip = ring({{0, 0}, {10, 0}, {10, 10}, {0, 10}});
        auto spiked = ring({{1, 1}, {9, 1}, {5, 1}, {5, 9}, {1, 9}});
        geom::ClipResult r;
        std::string err = geom::clipPolygonByConvex(spiked, clip, eps, r);
        CHECK(err.find("INVALID_SUBJECT") != std::string::npos);
    }

    // ---- 13. 旋转正方形裁剪器(凸): 包含关系与方向 ----
    {
        // 菱形裁剪器 |x|+|y| <= 10
        auto diamond = ring({{0, -10}, {10, 0}, {0, 10}, {-10, 0}});
        // 主体为轴对齐正方形 [ -3, 3 ]^2, 完全在菱形内
        auto subj = ring({{-3, -3}, {3, -3}, {3, 3}, {-3, 3}});
        geom::ClipResult r;
        CHECK(geom::clipPolygonByConvex(subj, diamond, eps, r).empty());
        CHECK(r.kind == geom::ResultKind::Polygon);
        CHECK(approx(r.area, 36.0, 1e-8));
        CHECK(geom::signedArea(r.vertices) > 0);
        for (const auto& p : r.vertices)
            CHECK(geom::pointInConvexCCW(p, diamond, eps));
    }

    // ---- 14. 面积界: 部分相交 ----
    {
        auto clip = ring({{0, 0}, {10, 0}, {10, 10}, {0, 10}});
        auto subj = ring({{5, 5}, {15, 5}, {15, 15}, {5, 15}});
        geom::ClipResult r;
        CHECK(geom::clipPolygonByConvex(subj, clip, eps, r).empty());
        CHECK(r.kind == geom::ResultKind::Polygon);
        CHECK(approx(r.area, 25.0, 1e-8));
    }

    std::printf("\nunit tests: %d checks, %d failure(s)\n", g_checks,
                g_failures);
    return g_failures == 0 ? 0 : 1;
}
