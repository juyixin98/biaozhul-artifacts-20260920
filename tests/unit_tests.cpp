// unit_tests.cpp — C++ 端确定性单元测试。
// 覆盖：真交叉（整数/分数交点）、各类端点接触、共线重叠/相触/分离、
//       零长线段（点）退化、大坐标（2^62 量级，验证 __int128 不溢出）、
//       以及交换 a<->b、c<->d、线段对交换后的结果不变性。
#include <cstdio>
#include <cstdlib>
#include <set>
#include <string>
#include <vector>

#include "geometry.hpp"
#include "json.hpp"

using namespace segint;

static int g_failures = 0;
static int g_checks = 0;

static void report_fail(const char* expr, const char* file, int line,
                        const std::string& extra = "") {
    ++g_failures;
    std::printf("FAIL %s:%d: %s %s\n", file, line, expr, extra.c_str());
}

#define CHECK(cond)                                                       \
    do {                                                                  \
        ++g_checks;                                                       \
        if (!(cond)) report_fail(#cond, __FILE__, __LINE__);              \
    } while (0)

#define CHECK_EQ(actual, expected)                                        \
    do {                                                                  \
        ++g_checks;                                                       \
        auto _a = (actual);                                               \
        auto _e = (expected);                                             \
        if (!(_a == _e)) {                                                \
            report_fail(#actual " == " #expected, __FILE__, __LINE__);    \
        }                                                                 \
    } while (0)

static std::string rat_str(const Rational& r) {
    return r.num_str() + "/" + r.den_str();
}

static std::set<std::string> hit_set(const ContactPoint& p) {
    return std::set<std::string>(p.hits.begin(), p.hits.end());
}

static void expect_cls(const Result& r, Classify c) {
    CHECK(r.cls == c);
}

static void expect_point(const Result& r, size_t idx,
                         const std::string& nx, const std::string& dx,
                         const std::string& ny, const std::string& dy,
                         std::set<std::string> hits) {
    CHECK(r.points.size() > idx);
    if (r.points.size() <= idx) return;
    const ContactPoint& p = r.points[idx];
    CHECK_EQ(p.x.num_str(), nx);
    CHECK_EQ(p.x.den_str(), dx);
    CHECK_EQ(p.y.num_str(), ny);
    CHECK_EQ(p.y.den_str(), dy);
    CHECK(hit_set(p) == hits);
}

// 序列化结果的规范化指纹（仅几何不变量：分类 + 点坐标）。
// 注意：端点交换后 hits 标签语义改变，故不纳入指纹；
// hits 的正确性由 Python 参考实现对每个变体逐一校验。
static std::string fingerprint(const Result& r) {
    std::string s = classify_name(r.cls);
    s += "|";
    std::vector<std::string> pts;
    for (const auto& p : r.points) {
        pts.push_back(rat_str(p.x) + "," + rat_str(p.y));
    }
    std::sort(pts.begin(), pts.end());
    for (const auto& t : pts) { s += t + ";"; }
    return s;
}

static Point P(long long x, long long y) { return Point{x, y}; }

static void test_basic_cross() {
    // (0,0)-(4,4) x (0,4)-(4,0) => (2,2)
    Result r = intersect(P(0, 0), P(4, 4), P(0, 4), P(4, 0));
    expect_cls(r, Classify::Cross);
    expect_point(r, 0, "2", "1", "2", "1", {});

    // (0,0)-(2,2) x (0,3)-(3,0) => (1.5,1.5)
    r = intersect(P(0, 0), P(2, 2), P(0, 3), P(3, 0));
    expect_cls(r, Classify::Cross);
    expect_point(r, 0, "3", "2", "3", "2", {});

    // 分数约简：(0,0)-(3,1) x (0,1)-(3,0) => (3/2,1/2)
    r = intersect(P(0, 0), P(3, 1), P(0, 1), P(3, 0));
    expect_cls(r, Classify::Cross);
    expect_point(r, 0, "3", "2", "1", "2", {});

    // 延长线相交但线段不相交 => none
    r = intersect(P(0, 0), P(1, 1), P(2, 0), P(3, -1));
    expect_cls(r, Classify::None);
    CHECK(r.points.empty());

    // 平行不共线
    r = intersect(P(0, 0), P(1, 1), P(0, 1), P(1, 2));
    expect_cls(r, Classify::None);
}

static void test_touch() {
    // T 型：c 落在 ab 内部
    Result r = intersect(P(0, 0), P(4, 0), P(2, 0), P(2, 3));
    expect_cls(r, Classify::Touch);
    expect_point(r, 0, "2", "1", "0", "1", {"c"});

    // 端点 a 顶在 cd 内部
    r = intersect(P(2, 0), P(0, 0), P(2, 2), P(2, -2));
    expect_cls(r, Classify::Touch);
    expect_point(r, 0, "2", "1", "0", "1", {"a"});

    // 端点-端点接触
    r = intersect(P(0, 0), P(1, 0), P(1, 0), P(2, 0));
    expect_cls(r, Classify::Touch);
    expect_point(r, 0, "1", "1", "0", "1", {"b", "c"});

    // 斜线段端点落在另一条水平线段内部
    r = intersect(P(0, 0), P(4, 0), P(2, 0), P(0, -3));
    expect_cls(r, Classify::Touch);
    expect_point(r, 0, "2", "1", "0", "1", {"c"});
}

static void test_collinear() {
    // 部分重叠（x 轴）：a=0,b=4,c=2,d=6 => 重叠区间 [2,4]
    Result r = intersect(P(0, 0), P(4, 0), P(2, 0), P(6, 0));
    expect_cls(r, Classify::Overlap);
    CHECK(r.points.size() == 2);
    expect_point(r, 0, "2", "1", "0", "1", {"c"});
    expect_point(r, 1, "4", "1", "0", "1", {"b"});

    // 完全包含
    r = intersect(P(0, 0), P(10, 0), P(2, 0), P(4, 0));
    expect_cls(r, Classify::Overlap);
    CHECK(r.points.size() == 2);
    expect_point(r, 0, "2", "1", "0", "1", {"c"});
    expect_point(r, 1, "4", "1", "0", "1", {"d"});

    // 两条线段完全相同
    r = intersect(P(1, 1), P(5, 5), P(1, 1), P(5, 5));
    expect_cls(r, Classify::Overlap);
    CHECK(r.points.size() == 2);
    expect_point(r, 0, "1", "1", "1", "1", {"a", "c"});
    expect_point(r, 1, "5", "1", "5", "1", {"b", "d"});

    // 共线仅端点相触（不算 overlap）
    r = intersect(P(0, 0), P(2, 0), P(2, 0), P(5, 0));
    expect_cls(r, Classify::Touch);
    CHECK(r.points.size() == 1);
    expect_point(r, 0, "2", "1", "0", "1", {"b", "c"});

    // 共线分离
    r = intersect(P(0, 0), P(2, 0), P(3, 0), P(5, 0));
    expect_cls(r, Classify::None);

    // y 轴方向（dx=0）的重叠
    r = intersect(P(0, 0), P(0, 5), P(0, 3), P(0, 8));
    expect_cls(r, Classify::Overlap);
    expect_point(r, 0, "0", "1", "3", "1", {"c"});
    expect_point(r, 1, "0", "1", "5", "1", {"b"});
}

static void test_zero_length() {
    // 点-点相同：四个端点坐标重合，全部列出
    Result r = intersect(P(3, 3), P(3, 3), P(3, 3), P(3, 3));
    expect_cls(r, Classify::Touch);
    expect_point(r, 0, "3", "1", "3", "1", {"a", "b", "c", "d"});

    // 点-点不同
    r = intersect(P(3, 3), P(3, 3), P(3, 4), P(3, 4));
    expect_cls(r, Classify::None);

    // 点在线段内部（共线）：零长线段两端点 a,b 重合均列出
    r = intersect(P(2, 2), P(2, 2), P(0, 2), P(4, 2));
    expect_cls(r, Classify::Touch);
    expect_point(r, 0, "2", "1", "2", "1", {"a", "b"});

    // 点在线段端点
    r = intersect(P(0, 2), P(0, 2), P(0, 2), P(4, 2));
    expect_cls(r, Classify::Touch);
    expect_point(r, 0, "0", "1", "2", "1", {"a", "b", "c"});

    // 点不在直线上
    r = intersect(P(1, 1), P(1, 1), P(0, 0), P(4, 0));
    expect_cls(r, Classify::None);

    // 点在直线上但延长线外
    r = intersect(P(5, 0), P(5, 0), P(0, 0), P(4, 0));
    expect_cls(r, Classify::None);

    // 第二条为零长，点落在第一条内部
    r = intersect(P(0, 0), P(4, 4), P(2, 2), P(2, 2));
    expect_cls(r, Classify::Touch);
    expect_point(r, 0, "2", "1", "2", "1", {"c", "d"});

    // 第二条为零长且与第一条的端点重合
    r = intersect(P(0, 0), P(4, 4), P(4, 4), P(4, 4));
    expect_cls(r, Classify::Touch);
    expect_point(r, 0, "4", "1", "4", "1", {"b", "c", "d"});
}

static void test_large_coordinates() {
    constexpr long long M = (1LL << 62) - 2;

    // 大坐标真交叉：(-M,-M)-(M,M) x (-M,M)-(M,-M) => (0,0)
    Result r = intersect(P(-M, -M), P(M, M), P(-M, M), P(M, -M));
    expect_cls(r, Classify::Cross);
    expect_point(r, 0, "0", "1", "0", "1", {});

    // 大坐标下交点分数坐标：用 ( -M, -M+3) 之类构造，只验证能分类且无异常。
    r = intersect(P(-M, -M), P(M, M - 2), P(-M, M), P(M, -M + 2));
    CHECK(r.cls == Classify::Cross);
    CHECK(r.points.size() == 1);
    // 交点应非常接近原点（分母为 M 量级），坐标在 [-3,3] 范围内。
    cpp_int nx(r.points[0].x.num_str());
    cpp_int dx(r.points[0].x.den_str());
    cpp_int ny(r.points[0].y.num_str());
    cpp_int dy(r.points[0].y.den_str());
    CHECK(dx > 0);
    CHECK(dy > 0);
    CHECK(nx <= 3 * dx && nx >= -3 * dx);
    CHECK(ny <= 3 * dy && ny >= -3 * dy);

    // 大坐标共线包含
    r = intersect(P(-M, 0), P(M, 0), P(-M / 2, 0), P(M / 2, 0));
    expect_cls(r, Classify::Overlap);
    expect_point(r, 0, std::to_string(-(M / 2)), "1", "0", "1", {"c"});
    expect_point(r, 1, std::to_string(M / 2), "1", "0", "1", {"d"});

    // 大坐标不相交（包围盒分离）
    r = intersect(P(-M, -M), P(-M + 10, -M + 10), P(M, M), P(M - 10, M - 10));
    expect_cls(r, Classify::None);

    // 大坐标端点接触
    r = intersect(P(M, M), P(M - 5, M - 5), P(M, M), P(M + 0, M - 9));
    expect_cls(r, Classify::Touch);
    expect_point(r, 0, std::to_string(M), "1", std::to_string(M), "1", {"a", "c"});

    // 近乎平行但不相交：cd 两端点都在 ab 同侧（s<0），不能误判
    r = intersect(P(-M, -M), P(M, M), P(-M, -M + 1), P(M - 2, M));
    expect_cls(r, Classify::None);

    // 近乎平行且真交叉：叉积 den = 6M-2，而坐标差中含 1 量级的量，
    // 参数 s=2M/(6M-2)、t=(2M-2)/(6M-2) 均在 (0,1)，对定向精度最敏感。
    r = intersect(P(-M, 0), P(M, 2), P(-M + 1, 1), P(M, 0));
    expect_cls(r, Classify::Cross);
    CHECK(r.points.size() == 1);
    // y 坐标 = 2*s = 4M/(6M-2) ∈ (0,1)
    cpp_int yy = r.points[0].y.num;
    cpp_int yd = r.points[0].y.den;
    CHECK(yy > 0 && yy < yd);
}

static void test_swap_invariance() {
    struct Case { Point a, b, c, d; };
    std::vector<Case> cases = {
        {P(0, 0), P(4, 4), P(0, 4), P(4, 0)},
        {P(0, 0), P(2, 2), P(0, 3), P(3, 0)},
        {P(0, 0), P(4, 0), P(2, 0), P(2, 3)},
        {P(0, 0), P(4, 0), P(2, 0), P(6, 0)},
        {P(1, 1), P(5, 5), P(1, 1), P(5, 5)},
        {P(2, 2), P(2, 2), P(0, 2), P(4, 2)},
        {P(3, 3), P(3, 3), P(3, 3), P(3, 3)},
        {P(-7, 3), P(5, -2), P(-2, -5), P(6, 4)},
    };
    for (const auto& cs : cases) {
        std::string base = fingerprint(intersect(cs.a, cs.b, cs.c, cs.d));
        CHECK(fingerprint(intersect(cs.b, cs.a, cs.c, cs.d)) == base);
        CHECK(fingerprint(intersect(cs.a, cs.b, cs.d, cs.c)) == base);
        CHECK(fingerprint(intersect(cs.b, cs.a, cs.d, cs.c)) == base);
        CHECK(fingerprint(intersect(cs.c, cs.d, cs.a, cs.b)) == base);
        CHECK(fingerprint(intersect(cs.d, cs.c, cs.b, cs.a)) == base);
    }
}

static void test_json() {
    JVal v = json_parse("{\"entries\":[{\"id\":7,\"a\":{\"x\":0,\"y\":0}}]}");
    CHECK(v.type == JVal::Object);
    CHECK_EQ(v.at("entries").as_array().size(), size_t(1));
    CHECK_EQ(v.at("entries").as_array()[0].at("id").as_int(), int64_t(7));
    CHECK_EQ(v.at("entries").as_array()[0].at("a").at("x").as_int(), int64_t(0));

    bool threw = false;
    try { (void)json_parse("{\"x\":1.5}"); } catch (const JError&) { threw = true; }
    CHECK(threw);

    threw = false;
    try { (void)json_parse("[1,2,"); } catch (const JError&) { threw = true; }
    CHECK(threw);

    JVal o = JVal::make_object();
    o.set("s", JVal::make_string("a\nb"));
    o.set("n", JVal::make_int(-3));
    std::string dumped = json_dump(o);
    JVal back = json_parse(dumped);
    CHECK_EQ(back.at("s").as_string(), std::string("a\nb"));
    CHECK_EQ(back.at("n").as_int(), int64_t(-3));
}

int main() {
    test_basic_cross();
    test_touch();
    test_collinear();
    test_zero_length();
    test_large_coordinates();
    test_swap_invariance();
    test_json();

    std::printf("checks: %d, failures: %d\n", g_checks, g_failures);
    if (g_failures) {
        std::printf("UNIT TESTS FAILED\n");
        return EXIT_FAILURE;
    }
    std::printf("UNIT TESTS PASSED\n");
    return EXIT_SUCCESS;
}
