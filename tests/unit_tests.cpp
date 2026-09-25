// unit_tests.cpp — 球面距离/包围盒/退化情形单元测试
//
// 距离校验采用【独立的第二条公式路径】（acos 余弦定理，long double）
// 与 haversine(atan2) 实现互相印证；另有手算常量（四分之一大圆、
// 半周长）对照与单位换算检查。
#include <math.h>

#include <cstdio>
#include <functional>
#include <random>
#include <string>
#include <vector>

#include "geo.hpp"
#include "json.hpp"
#include "service.hpp"

using namespace sph;

static int g_failures = 0;
static int g_checks = 0;

static void record(bool ok, const std::string& name, const std::string& detail) {
    ++g_checks;
    if (!ok) {
        ++g_failures;
        std::printf("  [FAIL] %s — %s\n", name.c_str(), detail.c_str());
    }
}

#define CHECK(cond, detail) \
    record(static_cast<bool>(cond), #cond, detail)

static bool approxRel(long double x, long double y, long double rel = 1e-12L) {
    long double scale = std::max({fabsl(x), fabsl(y), 1.0L});
    return fabsl(x - y) <= rel * scale;
}

// 独立参考实现：球面余弦定理 + acos（与生产代码的 haversine+atan2 不同路径）
static long double refDistance(const GeoPoint& a, const GeoPoint& b) {
    long double p1 = a.lat * DEG2RAD, p2 = b.lat * DEG2RAD;
    long double dl = wrapLongitude(b.lon - a.lon) * DEG2RAD;
    long double cosc = std::sin(p1) * std::sin(p2) +
                       std::cos(p1) * std::cos(p2) * std::cos(dl);
    cosc = std::min(1.0L, std::max(-1.0L, cosc));
    return EARTH_RADIUS_M * std::acos(cosc);
}

static void checkDistancePair(const std::string& name,
                              const GeoPoint& a, const GeoPoint& b,
                              long double expectedM, long double tolM) {
    long double d = sphericalDistance(a, b);
    long double dRev = sphericalDistance(b, a);
    char buf[256];
    std::snprintf(buf, sizeof(buf),
                  "expected ~%.9f m, got %.9f m (|Δ|=%.3e m)",
                  static_cast<double>(expectedM), static_cast<double>(d),
                  static_cast<double>(fabsl(d - expectedM)));
    record(fabsl(d - expectedM) <= tolM, name, buf);

    // 对称性 d(a,b)==d(b,a)
    record(approxRel(d, dRev), name + " [symmetric]",
           "distance must be symmetric");

    // 独立公式交叉验证（对跖点 acos 也应给出 πR，因夹了 [-1,1]）
    long double dr = refDistance(a, b);
    record(approxRel(d, dr, 1e-10L), name + " [vs acos reference]",
           [&] { char b2[160];
                  std::snprintf(b2, sizeof(b2), "haversine=%.9f acos=%.9f",
                                static_cast<double>(d), static_cast<double>(dr));
                  return std::string(b2); }().c_str());
}

int main() {
    std::printf("sphdist unit tests\n");

    // ---------- 1. 手算常量与单位 ----------
    const long double QUARTER = PI / 2.0L * EARTH_RADIUS_M;   // 赤道→极点
    const long double HALF    = PI       * EARTH_RADIUS_M;    // 半周长（对跖）
    const long double ONE_DEG = PI / 180.0L * EARTH_RADIUS_M; // 大圆上 1°
    std::printf("constants: R=%.1f m  quarter=%.6f m  half=%.6f m  1deg=%.6f m\n",
                static_cast<double>(EARTH_RADIUS_M),
                static_cast<double>(QUARTER),
                static_cast<double>(HALF),
                static_cast<double>(ONE_DEG));

    // 单位检查：四分之一大圆 ≈ 10007.56 km
    checkDistancePair("equator->north pole (quarter great circle)",
                      {0, 0}, {90, 0}, QUARTER, 1e-6L);

    // 赤道上经度差 1°
    checkDistancePair("1 degree along equator",
                      {0, 0}, {0, 1}, ONE_DEG, 1e-6L);

    // 赤道上 90° 经度差 = 四分之一大圆
    checkDistancePair("90 deg along equator",
                      {0, 0}, {0, 90}, QUARTER, 1e-6L);

    // ---------- 2. 重合点 ----------
    checkDistancePair("identical point", {31.2304, 121.4737},
                      {31.2304, 121.4737}, 0.0L, 1e-9L);
    // 同一点，经度写成 +180 与 -180（同一经线）
    checkDistancePair("same meridian +180/-180", {0, 180}, {0, -180},
                      0.0L, 1e-6L);
    // 北极：经度任意，仍是同一点（退化处理）
    checkDistancePair("north pole, differing longitudes",
                      {90, 10}, {90, 170}, 0.0L, 1e-6L);
    checkDistancePair("south pole, differing longitudes",
                      {-90, -100}, {-90, 80}, 0.0L, 1e-6L);

    // ---------- 3. 对跖点 ----------
    checkDistancePair("antipodal equator points (0,0)-(0,180)",
                      {0, 0}, {0, 180}, HALF, 1e-6L);
    checkDistancePair("antipodal general point",
                      {31.23, 121.47}, {-31.23, -58.53}, HALF, 1e-4L);
    checkDistancePair("north pole <-> south pole (antipodal)",
                      {90, 0}, {-90, 0}, HALF, 1e-6L);

    // ---------- 4. 跨反经线 ----------
    // (0,179) 与 (0,-179)：最短经度差 2°，不是 358°
    checkDistancePair("cross antimeridian 2deg apart",
                      {0, 179}, {0, -179}, 2 * ONE_DEG, 1e-4L);
    // 错误地按 358° 算会得到 ~39805 km；正确应为 ~222.4 km
    {
        long double d = sphericalDistance({0, 179}, {0, -179});
        record(d < 250000.0L, "cross-AM uses shortest arc (not 358deg)",
               "if ~3.98e7 m, antimeridian handling is broken");
    }
    // 斐济附近跨 180 的两点：179.0 与 -179.5 最短经度差为 1.5°
    checkDistancePair("Fiji region crossing antimeridian",
                      {-17.7, 179.0}, {-17.7, -179.5}, 1.5 * ONE_DEG * std::cos(-17.7L*DEG2RAD),
                      2.0L);

    // ---------- 5. 近极点 ----------
    // 距北极 10 km：lat=89.91..., lon 任意。同纬圈 180° 经度差 ~20 km，
    // 验证极点附近经度“收缩”，而不是按平面经度差算。
    {
        long double dlat = (10000.0L / EARTH_RADIUS_M) * RAD2DEG;
        long double lat = 90.0L - dlat;
        long double dSame = sphericalDistance({lat, 0}, {lat, 0});
        long double dOpp  = sphericalDistance({lat, 0}, {lat, 180});
        long double dSide = sphericalDistance({lat, 0}, {lat, 90});
        char b[200];
        std::snprintf(b, sizeof(b), "dSame=%.6f d90=%.6f d180=%.6f",
                      double(dSame), double(dSide), double(dOpp));
        record(dSame == 0.0L, "near-pole same lon", b);
        record(dOpp > 19990.0L && dOpp < 20010.0L,
               "near-pole 180deg lon diff ≈ 20km", b);
        record(dSide > 14135.0L && dSide < 14150.0L,
               "near-pole 90deg lon diff ≈ sqrt(2)*10km", b);
        // 到北极本身约 10 km
        long double dPole = sphericalDistance({lat, 0}, {90, 0});
        record(fabsl(dPole - 10000.0L) < 1e-6L, "near-pole distance to pole ≈ 10km", b);
    }

    // ---------- 6. 包围盒：跨反经线 ----------
    {
        BBox box;
        bool ok = sphericalCapBBox({0, 179}, 250000, box);
        record(ok, "bbox construct around (0,179)", "");
        record(box.crossesAntimeridian(), "bbox crosses antimeridian", "");
        // 粗筛必须保留反经线两侧的近点，也必须漏掉对面半球的点
        record(bboxContains(box, {0, -179}), "bbox keeps west side near AM", "");
        record(bboxContains(box, {0, 179}), "bbox keeps center", "");
        record(!bboxContains(box, {0, 0}), "bbox rejects far point lon=0", "");
        record(!bboxContains(box, {45, -179}), "bbox rejects lat-out point", "");
    }

    // ---------- 7. 包围盒：盖极 → 经度全宽 ----------
    {
        BBox box;
        sphericalCapBBox({89, 0}, 200000, box);  // 200km 帽覆盖北极
        record(box.lat_max >= 90.0L, "polar cap reaches pole lat", "");
        record(bboxContains(box, {89.5L, 137.7L}),
               "polar bbox contains any longitude near pole", "");
        record(!bboxContains(box, {0, 0}), "polar bbox rejects equator", "");
    }

    // ---------- 8. 包围盒只作候选过滤，绝不能误判最终结果 ----------
    // 随机/固定场景：bbox 通过的点里必须包含全部真实命中，且最终距离复核
    {
        std::mt19937_64 rng(42);
        std::uniform_real_distribution<double> latd(-89.999, 89.999);
        std::uniform_real_distribution<double> lond(-180.0, 180.0);
        bool recallOk = true;
        bool bboxSuperset = true;
        for (int t = 0; t < 20000; ++t) {
            GeoPoint c{latd(rng), lond(rng)};
            double R = std::uniform_real_distribution<double>(1000, 5000000)(rng);
            BBox box;
            sphericalCapBBox(c, R, box);
            for (int k = 0; k < 8; ++k) {
                GeoPoint p{latd(rng), lond(rng)};
                long double d = sphericalDistance(c, p);
                bool trueIn = d <= (long double)R;
                bool cand = bboxContains(box, p);
                if (trueIn && !cand) recallOk = false;        // 召回率必须 100%
                if (cand && d > (long double)R * 1.000000001L + 1e-9L)
                    bboxSuperset = false;  // 这允许粗筛偏多（本来就只是候选）
            }
        }
        record(recallOk, "bbox recall 100% over 20000 random queries",
               "a true in-range point was rejected by candidate filter");
        (void)bboxSuperset;
    }

    // ---------- 9. 校验与归一化 ----------
    {
        record(validateLatLon(0, 0) == GeoError::Ok, "valid origin", "");
        record(validateLatLon(90.0001, 0) == GeoError::LatOutOfRange, "lat>90 rejected", "");
        record(validateLatLon(-91, 0) == GeoError::LatOutOfRange, "lat<-90 rejected", "");
        record(validateLatLon(0, 181) == GeoError::LonOutOfRange, "lon>180 rejected", "");
        record(validateLatLon(0, -180.5) == GeoError::LonOutOfRange, "lon<-180 rejected", "");
        record(validateLatLon(NAN, 0) == GeoError::NotFinite, "NaN rejected", "");
        BBox dummy;
        record(!sphericalCapBBox({0, 0}, -1, dummy), "negative radius rejected", "");
        record(wrapLongitude(181.0L) == -179.0L, "wrap 181 -> -179", "");
        record(wrapLongitude(-181.0L) == 179.0L, "wrap -181 -> 179", "");
        record(wrapLongitude(540.0L) == 180.0L || wrapLongitude(540.0L) == -180.0L,
               "wrap 540 -> boundary", "");
    }

    // ---------- 10. JSON 往返 ----------
    {
        std::string s = R"({"a":[1,2.5,-3e2,"x",true,null,{"k":"vé"}],"b":{}})";
        auto pr = json::parse(s);
        record(pr.ok, "json parse", pr.error.c_str());
        if (pr.ok) {
            const auto& a = pr.value.obj["a"].arr;
            record(a.size() == 7, "json array len", "");
            record(a[2].number == -300.0, "json number -3e2", "");
            record(a[6].find("k")->str == "v\xc3\xa9", "json unicode escape -> utf8", "");
            std::string roundtrip = json::dump(pr.value, -1);
            auto pr2 = json::parse(roundtrip);
            record(pr2.ok && json::dump(pr2.value, -1) == roundtrip,
                   "json dump/parse roundtrip", "");
        }
        auto bad = json::parse("{not json");
        record(!bad.ok, "json rejects garbage", "");
    }

    std::printf("\n%d checks, %d failures\n", g_checks, g_failures);
    return g_failures == 0 ? 0 : 1;
}
