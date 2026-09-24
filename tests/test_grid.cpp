#include <cmath>
#include <cstdio>
#include <string>
#include <vector>

#include "gridfusion/grid_map.hpp"
#include "gridfusion/raycast.hpp"
#include "gridfusion/sha256.hpp"
#include "gridfusion/types.hpp"

using namespace gridfusion;

static int failures = 0;
#define CHECK_GF(cond)                                                       \
    do {                                                                  \
        if (!(cond)) {                                                    \
            std::printf("FAIL %s:%d  %s\n", __FILE__, __LINE__, #cond);  \
            ++failures;                                                   \
        }                                                                 \
    } while (0)

static bool approx(double a, double b, double tol = 1e-9) {
    return std::fabs(a - b) <= tol;
}

static MapConfig makeCfg(int w, int h, double res = 1.0, double ox = 0.0,
                         double oy = 0.0) {
    MapConfig c;
    c.resolution = res;
    c.origin_x = ox;
    c.origin_y = oy;
    c.width = w;
    c.height = h;
    return c;
}

static Beam beam(double angle, double range, bool nr = false) {
    return Beam{angle, range, nr};
}

static std::vector<RayEvent>
trace(bool ref, double sx, double sy, double dx, double dy, double travel,
      const MapConfig& c, bool no_return = false) {
    std::vector<RayEvent> ev;
    auto sink = [&](const RayEvent& e) { ev.push_back(e); };
    bool clipped;
    if (ref)
        clipped = traceBeamSampled(sx, sy, dx, dy, travel, c.width, c.height,
                                   c.origin_x, c.origin_y, c.resolution,
                                   no_return, sink);
    else
        clipped = traceBeamAmanatidesWoo(
            sx, sy, dx, dy, travel, c.width, c.height, c.origin_x,
            c.origin_y, c.resolution, no_return, sink);
    ev.emplace_back(RayEvent{-1 - (clipped ? 1 : 0), 0, 0, -1});
    return ev;
}

// Both traversals must emit identical event sequences for one ray.
static void checkSame(const char* name, double sx, double sy, double ang,
                      double travel, const MapConfig& c, bool no_return) {
    const double dx = std::cos(ang), dy = std::sin(ang);
    auto a = trace(false, sx, sy, dx, dy, travel, c, no_return);
    auto b = trace(true, sx, sy, dx, dy, travel, c, no_return);
    bool same = a.size() == b.size();
    for (size_t i = 0; same && i < a.size(); ++i)
        same = a[i].index == b[i].index && a[i].occupied == b[i].occupied;
    if (!same) {
        std::printf("FAIL traversal mismatch: %s\n", name);
        for (auto& e : a) std::printf("  AW  idx=%d occ=%d\n", e.index, e.occupied);
        for (auto& e : b) std::printf("  ref idx=%d occ=%d\n", e.index, e.occupied);
        ++failures;
    }
}

static void testWallHit() {
    MapConfig c = makeCfg(10, 10);
    GridMap m(c);
    Scan s;
    s.pose = Pose{0.5, 0.5, 0};
    s.beams.push_back(beam(0, 3.0));  // wall 3 m ahead
    ScanStats st = m.update(s);

    CHECK_GF(st.hits == 1);
    CHECK_GF(st.no_returns == 0);
    CHECK_GF(st.beams_clipped == 0);
    // Cells (0,0),(1,0),(2,0) free; (3,0) occupied.
    for (int x = 0; x < 3; ++x) {
        CHECK_GF(approx(m.cell(x, 0).logit, c.l_free));
        CHECK_GF(m.cell(x, 0).observed);
    }
    CHECK_GF(approx(m.cell(3, 0).logit, c.l_occ));
    CHECK_GF(m.cell(4, 0).observed == false);  // beyond endpoint: unknown
    CHECK_GF(st.free_updates == 3);
    CHECK_GF(st.occupied_updates == 1);
    // Probability sanity.
    auto p = m.probabilities();
    const double pOcc = 1.0 / (1.0 + std::exp(-c.l_occ));
    const double pFree = 1.0 / (1.0 + std::exp(-c.l_free));
    CHECK_GF(approx(p[3], pOcc, 1e-6));
    CHECK_GF(approx(p[0], pFree, 1e-6));
    CHECK_GF(approx(p[99], 0.5, 1e-12));  // unobserved exports as 0.5
}

static void testUnknownVsObservedHalf() {
    // Sensor model chosen so a free then a hit cancel exactly to logit 0.
    MapConfig c = makeCfg(5, 5);
    c.l_occ = 0.5;
    c.l_free = -0.5;
    GridMap m(c);
    CHECK_GF(!m.cell(1, 1).observed);
    Scan s;
    s.pose = Pose{0.5, 0.5, 0};
    s.beams.push_back(beam(0, 2.0));  // (0,0),(1,0) free; (2,0) hit
    m.update(s);
    Scan s2;
    s2.pose = Pose{2.5, 0.5, M_PI};  // looking left
    s2.beams.push_back(beam(0, 2.0));  // (2,0),(1,0) free; (0,0) hit
    m.update(s2);
    // (1,0) was free(-0.5) then free(-0.5) -> -1. (0,0)/(2,0): -0.5+0.5=0.
    CHECK_GF(approx(m.cell(0, 0).logit, 0.0));
    CHECK_GF(m.cell(0, 0).observed);  // observed at exactly 0.5 ...
    CHECK_GF(approx(m.probabilities()[0], 0.5, 1e-12));
    CHECK_GF(!m.cell(4, 4).observed);  // ... distinct from never-observed
}

static void testSaturation() {
    MapConfig c = makeCfg(5, 5);
    GridMap m(c);
    Scan hit;
    hit.pose = Pose{0.5, 0.5, 0};
    hit.beams.push_back(beam(0, 3.0));
    for (int i = 0; i < 100; ++i) m.update(hit);
    // Occupied endpoint must be bounded by l_max, frees by l_min.
    CHECK_GF(approx(m.cell(3, 0).logit, c.l_max));
    CHECK_GF(approx(m.cell(0, 0).logit, c.l_min));
    CHECK_GF(m.cell(3, 0).logit <= static_cast<float>(c.l_max) + 1e-6f);
    CHECK_GF(m.cell(0, 0).logit >= static_cast<float>(c.l_min) - 1e-6f);
    // One more update does not move the saturated values.
    m.update(hit);
    CHECK_GF(approx(m.cell(3, 0).logit, c.l_max));
    CHECK_GF(approx(m.cell(0, 0).logit, c.l_min));
}

static void testNoReturn() {
    MapConfig c = makeCfg(8, 8);
    c.default_max_range = 2.0;
    GridMap m(c);
    Scan s;
    s.pose = Pose{3.5, 3.5, 0};
    s.beams.push_back(beam(0, 0.0, true));   // no return, cap 2 m
    s.beams.push_back(beam(M_PI, 0.0, true));
    ScanStats st = m.update(s);
    CHECK_GF(st.no_returns == 2);
    CHECK_GF(st.hits == 0);
    CHECK_GF(st.occupied_updates == 0);
    // Every traversed cell is free. East beam crosses {3,4,5}, west beam
    // {3,2,1}; the shared sensor cell is updated twice.
    CHECK_GF(approx(m.cell(3, 3).logit, 2 * c.l_free));
    CHECK_GF(approx(m.cell(4, 3).logit, c.l_free));
    CHECK_GF(approx(m.cell(5, 3).logit, c.l_free));
    CHECK_GF(approx(m.cell(2, 3).logit, c.l_free));
    CHECK_GF(approx(m.cell(1, 3).logit, c.l_free));
    CHECK_GF(!m.cell(6, 3).observed);
    CHECK_GF(st.beams_clipped == 0);
}

static void testBoundary() {
    MapConfig c = makeCfg(5, 5);
    c.default_max_range = 2.5;
    GridMap m(c);

    // Returned beam whose endpoint lies OUTSIDE the map: clip to boundary,
    // only frees, no occupied event anywhere.
    Scan s;
    s.pose = Pose{2.5, 2.5, 0};
    s.beams.push_back(beam(0, 10.0));
    ScanStats st = m.update(s);
    CHECK_GF(st.beams_clipped == 1);
    CHECK_GF(st.occupied_updates == 0);
    for (int x = 2; x <= 4; ++x) CHECK_GF(approx(m.cell(x, 2).logit, c.l_free));
    CHECK_GF(!m.cell(0, 0).observed);

    // No-return beam reaching the boundary line exactly (x = 5.0).
    Scan s2;
    s2.pose = Pose{2.5, 0.5, 0};
    s2.beams.push_back(beam(0, 0.0, true));  // max 2.5 -> ends at x=5.0
    ScanStats st2 = m.update(s2);
    CHECK_GF(st2.beams_clipped == 1);
    CHECK_GF(st2.occupied_updates == 0);
    for (int x = 2; x <= 4; ++x) CHECK_GF(approx(m.cell(x, 0).logit, c.l_free));

    // Boundary-exact interior endpoint belongs to the next half-open cell.
    MapConfig c2 = makeCfg(6, 3);
    GridMap m2(c2);
    Scan s3;
    s3.pose = Pose{0.5, 0.5, 0};
    s3.beams.push_back(beam(0, 2.5));  // endpoint (3.0,0.5) -> cell (3,0)
    auto st3 = m2.update(s3);
    CHECK_GF(st3.beams_clipped == 0);
    CHECK_GF(approx(m2.cell(3, 0).logit, c2.l_occ));
    CHECK_GF(approx(m2.cell(2, 0).logit, c2.l_free));
}

static void testZeroLength() {
    MapConfig c = makeCfg(4, 4);
    GridMap m(c);
    Scan s;
    s.pose = Pose{1.5, 1.5, 0};
    s.beams.push_back(beam(0.77, 0.0));  // zero-range return: hit at sensor
    ScanStats st = m.update(s);
    CHECK_GF(st.occupied_updates == 1);
    CHECK_GF(approx(m.cell(1, 1).logit, c.l_occ));
}

static void testVersionBinding() {
    MapConfig a = makeCfg(10, 10, 0.1, -1.0, -1.0);
    MapConfig b = makeCfg(10, 10, 0.2, -1.0, -1.0);  // resolution changed
    MapConfig c = makeCfg(10, 10, 0.1, -1.0, -2.0);  // origin changed
    MapConfig d = makeCfg(10, 10, 0.1, -1.0, -1.0);
    d.l_occ = 1.0;  // sensor-only change: geometry unchanged
    const std::string va = GridMap::make_version_id(a);
    CHECK_GF(va.size() == 64);
    CHECK_GF(va == GridMap::make_version_id(a));      // deterministic
    CHECK_GF(va != GridMap::make_version_id(b));      // resolution bound
    CHECK_GF(va != GridMap::make_version_id(c));      // origin bound
    CHECK_GF(va == GridMap::make_version_id(d));      // sensor params excluded
    // It really is the SHA-256 of the canonical geometry string.
    CHECK_GF(va == sha256::hex(GridMap::canonical_geometry(a)));
    CHECK_GF(!MapConfig().validate().empty());  // default (0x0) is invalid
    CHECK_GF(a.validate().empty());
}

static void testTraversalAgreement() {
    MapConfig c = makeCfg(7, 7);
    // Exact 45-degree beam through grid corners: tie must be diagonal.
    checkSame("corner-tie hit", 0.5, 0.5, M_PI / 4, 3.0 * std::sqrt(2.0),
              c, false);
    checkSame("corner-tie no-return", 0.5, 0.5, M_PI / 4, 2.0 * std::sqrt(2.0),
              c, true);
    checkSame("horizontal", 3.5, 3.5, 0, 3.0, c, false);
    checkSame("vertical", 3.5, 3.5, M_PI / 2, 2.2, c, false);
    checkSame("reverse diag", 5.5, 5.5, -3.0 * M_PI / 4, 4.0 * std::sqrt(2.0),
              c, false);
    checkSame("odd angle", 3.1, 2.7, 0.37, 3.41, c, false);
    checkSame("odd angle no-return", 3.1, 2.7, 0.37, 3.41, c, true);
    checkSame("boundary-exact end", 0.5, 0.5, 0, 3.5, c, false);

    // Verify the corner-tie event content directly: only diagonal cells.
    std::vector<RayEvent> ev;
    traceBeamAmanatidesWoo(0.5, 0.5, std::sqrt(0.5), std::sqrt(0.5),
                           3.0 * std::sqrt(2.0), 7, 7, 0, 0, 1.0, false,
                           [&](const RayEvent& e) { ev.push_back(e); });
    const int expected[4][2] = {{0, 0}, {1, 1}, {2, 2}, {3, 3}};
    CHECK_GF(ev.size() == 4);
    for (size_t i = 0; i < ev.size(); ++i) {
        CHECK_GF(ev[i].cx == expected[i][0] && ev[i].cy == expected[i][1]);
        CHECK_GF(ev[i].occupied == (i == 3));
    }
}

static void testReferenceFusion() {
    MapConfig c = makeCfg(12, 12, 0.5, -3.0, -3.0);
    std::vector<Scan> scans;
    for (int k = 0; k < 6; ++k) {
        Scan s;
        const double a = k * M_PI / 3;
        s.pose = Pose{0.0, 0.0, a};
        for (int b = 0; b < 13; ++b) {
            const double ang = -M_PI / 2 + b * M_PI / 12;
            // Deterministic fake "walls": range depends on bearing.
            const double r = 2.5 + 1.5 * std::cos(3.0 * ang + k);
            if (b == 4)
                s.beams.push_back(Beam{ang, 0.0, true});  // a no-return beam
            else
                s.beams.push_back(Beam{ang, r, false});
        }
        scans.push_back(std::move(s));
    }
    // Include a couple of boundary-clipping beams.
    scans.back().beams.push_back(Beam{0.2, 50.0, false});
    scans.back().beams.push_back(Beam{-0.4, 0.0, true});

    GridMap::ReferenceDiff d =
        GridMap::verifyAgainstReference(c, scans);
    if (d.differing_cells != 0) {
        std::printf("FAIL reference fusion: %d/%d cells differ, max %g\n",
                    d.differing_cells, d.cell_count, d.max_abs_logit_diff);
        ++failures;
    }
}

static void testRepeatedScans() {
    // Two scans from the same pose: identical updates, monotone convergence.
    MapConfig c = makeCfg(6, 2);
    GridMap m(c);
    Scan s;
    s.pose = Pose{0.5, 0.5, 0};
    s.beams.push_back(beam(0, 4.0));
    auto st1 = m.update(s);
    auto st2 = m.update(s);
    CHECK_GF(st1.occupied_updates == st2.occupied_updates);
    CHECK_GF(approx(m.cell(4, 0).logit, 2 * c.l_occ));
    CHECK_GF(approx(m.cell(1, 0).logit, 2 * c.l_free));
}

static void testModelConstants() {
    // Defaults must actually be logits of the documented probabilities.
    CHECK_GF(approx(0.8473, std::log(0.7 / 0.3), 1e-3));
    CHECK_GF(approx(-0.4055, std::log(0.4 / 0.6), 1e-3));
    CHECK_GF(approx(logitFromProbability(0.5), 0.0));
}

int main() {
    testWallHit();
    testUnknownVsObservedHalf();
    testSaturation();
    testNoReturn();
    testBoundary();
    testZeroLength();
    testVersionBinding();
    testTraversalAgreement();
    testReferenceFusion();
    testRepeatedScans();
    testModelConstants();
    if (failures) {
        std::printf("%d grid check(s) failed\n", failures);
        return 1;
    }
    std::printf("all grid tests passed\n");
    return 0;
}
