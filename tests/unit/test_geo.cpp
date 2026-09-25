// 单元测试：手算大圆距离对照 + 退化情形 + 候选包围盒性质 + JSON 解析。
// 无第三方框架，失败时打印并用非零退出码结束。

#include <algorithm>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <limits>
#include <string>
#include <vector>

#include "geo.h"
#include "json.h"

namespace {

int g_failures = 0;
int g_checks = 0;
constexpr long double PI = 3.141592653589793238462643383279502884L;

void check(bool cond, const std::string& name) {
    ++g_checks;
    if (cond) {
        std::printf("[PASS] %s\n", name.c_str());
    } else {
        ++g_failures;
        std::printf("[FAIL] %s\n", name.c_str());
    }
}

bool nearRel(long double got, long double want, long double relTol,
             long double absTol = 1e-9L) {
    return std::fabs(got - want) <=
           std::max(absTol, relTol * std::max(1.0L, std::fabs(want)));
}

geo::LatLon ll(long double lat, long double lon) {
    geo::LatLon p{lat, lon};
    std::string err;
    if (!geo::normalize(p, &err)) {
        std::printf("normalize error: %s\n", err.c_str());
        std::exit(2);
    }
    return p;
}

// 球面余弦定理，独立实现，用来交叉验证 haversine。
long double cosLawAngle(const geo::LatLon& a, const geo::LatLon& b) {
    const long double d2r = PI / 180.0L;
    long double dl = b.lon - a.lon;
    while (dl > 180.0L) dl -= 360.0L;
    while (dl < -180.0L) dl += 360.0L;
    long double c = std::sin(a.lat * d2r) * std::sin(b.lat * d2r) +
                    std::cos(a.lat * d2r) * std::cos(b.lat * d2r) *
                        std::cos(dl * d2r);
    c = std::max(-1.0L, std::min(1.0L, c));
    return std::acos(c);
}

void testHandCalculatedDistances() {
    // 手算基准（R = 6371000 m）：
    //  1) 本初子午线 赤道→北极：距离 = pi*R/2 = 10007543.398010286
    //  2) 赤道上对跖 (0,0)→(0,180)：距离 = pi*R = 20015086.796020572
    //  3) 赤道上 1 度：距离 = R*pi/180 = 111194.92664455873
    //  4) 赤道上 0.0002 度：22.238985328911746 m
    //  5) 同子午线 30°N→60°N：30 度 = 3335847.7993367620
    //  6) 45° 方位已知点 (45N,45E)→(45N,135E)：
    //     h = 0 + cos²45·sin²45 = 0.25；角 = 2 asin(0.5) = pi/3；
    //     距离 = R*pi/3 = 6671695.598673524
    //  7) 反经线两侧 (0,179)→(0,-179)：2 度 = 222389.85328911746

    struct Case {
        geo::LatLon a, b;
        long double want;
        const char* name;
    };
    const long double quarter = PI * geo::EARTH_RADIUS_M / 2.0L;
    const long double hemi = PI * geo::EARTH_RADIUS_M;
    const long double deg1 = geo::EARTH_RADIUS_M * PI / 180.0L;
    std::vector<Case> cases = {
        {ll(0, 0), ll(90, 0), quarter, "hand: equator to north pole = pi R/2"},
        {ll(0, 0), ll(0, 180), hemi, "hand: antipodes on equator = pi R"},
        {ll(0, 0), ll(0, 1), deg1, "hand: one degree on equator"},
        {ll(0, 0), ll(0, 0.0002L), deg1 * 0.0002L,
         "hand: 0.0002 deg on equator (22.24 m)"},
        {ll(30, 0), ll(60, 0), deg1 * 30.0L,
         "hand: 30 deg meridian arc"},
        {ll(45, 45), ll(45, 135), hemi / 3.0L,
         "hand: (45,45)->(45,135) = pi R/3"},
        {ll(0, 179), ll(0, -179), deg1 * 2.0L,
         "hand: cross-antimeridian 2 deg"},
    };
    for (const auto& c : cases) {
        long double d = geo::distanceM(c.a, c.b);
        check(nearRel(d, c.want, 1e-12L, 1e-9L), c.name);
        // 对称：距离必须与方向无关。
        long double dRev = geo::distanceM(c.b, c.a);
        check(nearRel(d, dRev, 0.0L, 1e-12L),
              std::string(c.name) + " (symmetry)");
        // 与独立的球面余弦定理实现交叉对照。
        // 注意：acos 形式在小角度处有灾难性抵消，是参考实现的精度瓶颈
        // （不是本实现的），因此绝对容差放到 1e-12 rad（距离上约 6 µm）。
        long double wantAngle = cosLawAngle(c.a, c.b);
        long double gotAngle = geo::centralAngle(c.a, c.b);
        check(nearRel(gotAngle, wantAngle, 1e-15L, 1e-12L),
              std::string(c.name) + " (cross-check cosine law)");
    }
}

void testDegeneracies() {
    // 重合点：精确为 0
    check(geo::distanceM(ll(35.1, 139.7), ll(35.1, 139.7)) == 0.0L,
          "degenerate: identical points distance 0");

    // 对跖点族：距离必须恒为 piR，地心角恒为 pi
    struct A { geo::LatLon a, b; const char* n; };
    std::vector<A> antip = {
        {ll(0, 0), ll(0, 180), "antipode: equator 0/180"},
        {ll(0, 20), ll(0, -160), "antipode: equator across meridian"},
        {ll(40.7128, -74.006), ll(-40.7128, 105.994),
         "antipode: New York / Indian Ocean"},
        {ll(89.999999, 45), ll(-89.999999, -135),
         "antipode: near poles"},
    };
    for (const auto& c : antip) {
        long double ang = geo::centralAngle(c.a, c.b);
        long double d = geo::distanceM(c.a, c.b);
        check(nearRel(ang, PI, 0.0L, 1e-15L), std::string(c.n) + " angle=pi");
        check(nearRel(d, PI * geo::EARTH_RADIUS_M, 1e-15L, 1e-8L),
              std::string(c.n) + " distance=piR");
    }

    // 极点：经度不同不影响极点距离
    check(nearRel(geo::distanceM(ll(90, 0), ll(90, 90)), 0.0L, 0.0L, 1e-12L),
          "degenerate: same pole any longitude -> 0");
    // 北极 → 南极 = piR
    check(nearRel(geo::distanceM(ll(90, 0), ll(-90, 0)),
                  PI * geo::EARTH_RADIUS_M, 1e-15L, 1e-8L),
          "degenerate: north pole to south pole");
    // 北极 → 赤道任意点 = piR/2
    check(nearRel(geo::distanceM(ll(90, 0), ll(0, 123.45)),
                  PI * geo::EARTH_RADIUS_M / 2.0L, 1e-15L, 1e-8L),
          "degenerate: north pole to equator = pi R/2");
}

void testNormalizeAndValidation() {
    geo::LatLon p{90.0, 100.0};
    std::string err;
    check(geo::normalize(p, &err) && p.lon == 0.0,
          "normalize: longitude zeroed at pole");

    geo::LatLon p2{0.0, 180.0};
    check(geo::normalize(p2, &err) && p2.lon == -180.0,
          "normalize: lon 180 -> -180 canonical");

    geo::LatLon p3{90.0000000000005L, 0}; // 吸附范围内
    check(geo::normalize(p3, &err) && p3.lat == 90.0,
          "normalize: snap to pole within tolerance");

    geo::LatLon p4{91.0, 0};
    check(!geo::normalize(p4, &err) && err.find("latitude") != std::string::npos,
          "validate: latitude > 90 rejected");
    geo::LatLon p5{0, 200};
    check(!geo::normalize(p5, &err) && err.find("longitude") != std::string::npos,
          "validate: longitude > 180 rejected");
    geo::LatLon p6{std::nan(""), 0};
    check(!geo::normalize(p6, &err), "validate: NaN rejected");
    geo::LatLon p7{std::numeric_limits<long double>::infinity(), 0};
    check(!geo::normalize(p7, &err), "validate: infinity rejected");
}

void testCandidateBoxNeverMisses() {
    // 关键不变量：包围盒可误纳，绝不可漏掉真正在半径内的点。
    // 多组中心/半径 × 密集网格扫描验证。
    const long double deg = geo::EARTH_RADIUS_M * PI / 180.0L;
    std::vector<long double> centerLats = {
        0, 15, 45, -60, 80, -85, 89.9, -89.9};
    std::vector<long double> radii = {
        deg * 0.5, deg * 1.0, deg * 5.0, deg * 30.0,
        deg * 100.0, PI * geo::EARTH_RADIUS_M};

    std::vector<geo::LatLon> grid;
    for (int la = -89; la <= 89; la += 2) {
        for (int lo = -180; lo < 180; lo += 2) {
            grid.push_back(ll(static_cast<long double>(la),
                              static_cast<long double>(lo)));
        }
    }
    int missed = 0, falseCand = 0;
    const std::vector<long double> centerLons = {0.0L, 90.0L, 179.0L, -179.5L};
    bool printedFirstMiss = false;
    for (long double clat : centerLats) {
        for (long double clon : centerLons) {
            geo::LatLon c = ll(clat, clon);
            for (long double r : radii) {
                geo::CandidateBox box = geo::makeCandidateBox(c, r);
                for (const auto& q : grid) {
                    long double d = geo::distanceM(c, q);
                    // 真值判定与包围盒使用同一量级的裕量（角容差 1e-12 rad）。
                    bool inside = d <= r + geo::EARTH_RADIUS_M * 1e-12L;
                    bool cand = box.contains(q);
                    if (inside && !cand) {
                        ++missed;
                        if (!printedFirstMiss) {
                            std::printf("         [first miss] center=(%Lf,%Lf) "
                                        "r=%Lf m q=(%Lf,%Lf) d=%Lf m\n",
                                        clat, clon, r, q.lat, q.lon, d);
                            printedFirstMiss = true;
                        }
                    }
                    if (cand && !inside) ++falseCand;
                }
            }
        }
    }
    check(missed == 0, "candidate box: never excludes a true hit (grid scan)");
    check(falseCand >= 0, "candidate box: grid scan completed"); // 统计性记录
    std::printf("         (info) false-but-candidate grid cells: %d\n", falseCand);
}

void testRangeQuery() {
    const long double deg = geo::EARTH_RADIUS_M * PI / 180.0L;

    // 跨反经线：中心 (0,179)，半径 3 度
    {
        geo::LatLon c = ll(0, 179);
        std::vector<geo::LatLon> pts = {
            ll(0, -179),   // 2 度，命中
            ll(0, -176),   // 5 度，不命中
            ll(0, 176),    // 3 度，边界命中
            ll(0, 0),      // 远
            ll(89.9, 50),  // 远
        };
        std::size_t cand = 0;
        auto hits = geo::rangeQuery(c, 3.0 * deg + 1e-6L, pts, &cand);
        std::vector<std::size_t> idx;
        for (auto& h : hits) idx.push_back(h.index);
        std::sort(idx.begin(), idx.end());
        check(idx == std::vector<std::size_t>({0, 2}),
              "range: cross-antimeridian hits {idx0, idx2}");
        check(cand >= hits.size(), "range: candidate count >= hit count");
    }

    // 近极点：中心 89N，半径 5 度（包围盒应覆盖全部经度）
    {
        geo::LatLon c = ll(89, 0);
        std::vector<geo::LatLon> pts = {
            ll(89.5, 0), ll(88.0, 90), ll(89.5, 180),
            ll(80, 0), ll(-89, 0),
        };
        auto hits = geo::rangeQuery(c, 5.0 * deg + 1e-6L, pts);
        std::vector<std::size_t> idx;
        for (auto& h : hits) idx.push_back(h.index);
        std::sort(idx.begin(), idx.end());
        check(idx == std::vector<std::size_t>({0, 1, 2}),
              "range: near-pole covers all longitudes, hits {0,1,2}");
    }

    // 半径 = piR：所有点（含对跖点）都命中
    {
        geo::LatLon c = ll(10, 10);
        std::vector<geo::LatLon> pts = {ll(-10, -170), ll(0, 0), ll(-90, 0)};
        auto hits = geo::rangeQuery(c, PI * geo::EARTH_RADIUS_M, pts);
        check(hits.size() == 3, "range: radius pi R includes antipodes/all");
    }

    // 半径 0：只命中重合点
    {
        geo::LatLon c = ll(35.68, 139.76);
        std::vector<geo::LatLon> pts = {ll(35.68, 139.76), ll(35.69, 139.77)};
        auto hits = geo::rangeQuery(c, 0.0L, pts);
        check(hits.size() == 1 && hits[0].distance_m == 0.0L,
              "range: zero radius only identical point");
    }

    // 命中按输入索引顺序，距离非负且递增不做要求，但值必须正确
    {
        geo::LatLon c = ll(0, 0);
        std::vector<geo::LatLon> pts = {ll(0, 1), ll(0, -1)};
        auto hits = geo::rangeQuery(c, 5.0 * deg, pts);
        check(hits.size() == 2 && hits[0].index == 0 && hits[1].index == 1,
              "range: hits preserve input order");
        check(nearRel(hits[0].distance_m, deg, 1e-12L, 1e-9L) &&
                      nearRel(hits[1].distance_m, deg, 1e-12L, 1e-9L),
              "range: hit distances correct");
    }
}

void testJsonParser() {
    std::string err;
    auto r = json::parse(R"({"a": 1, "b": [true, false, null, -2.5e3, "x"]})",
                         &err);
    check(r != nullptr && r->is(json::Type::Object), "json: basic parse");
    check(r && r->find("a")->number == 1.0L, "json: number field");
    check(r && r->find("b")->arr.size() == 5, "json: array size");
    check(r && r->find("b")->arr[3].number == -2500.0L, "json: exponent");

    auto s = json::parse(R"("é中A")", &err); // é 中 A
    check(s != nullptr && s->str == "\xC3\xA9\xE4\xB8\xAD\x41",
          "json: unicode escape to UTF-8");

    auto pair = json::parse(R"("😀")", &err); // 😀
    check(pair != nullptr && pair->str == "\xF0\x9F\x98\x80",
          "json: surrogate pair (emoji)");

    check(json::parse("{,}", &err) == nullptr, "json: invalid object rejected");
    check(json::parse("123abc", &err) == nullptr, "json: trailing chars rejected");
    check(json::parse("\"unterminated", &err) == nullptr,
          "json: unterminated string rejected");
    check(json::parse("[1, 2,]", &err) == nullptr, "json: trailing comma rejected");

    auto dup = json::parse(R"({"k":1,"k":2})", &err);
    check(dup != nullptr && dup->find("k")->number == 2.0L,
          "json: duplicate key last wins");
}

} // namespace

int main() {
    testHandCalculatedDistances();
    testDegeneracies();
    testNormalizeAndValidation();
    testCandidateBoxNeverMisses();
    testRangeQuery();
    testJsonParser();

    std::printf("\n%d checks, %d failures\n", g_checks, g_failures);
    if (g_failures != 0) {
        std::printf("UNIT TESTS FAILED\n");
        return 1;
    }
    std::printf("ALL UNIT TESTS PASSED\n");
    return 0;
}
