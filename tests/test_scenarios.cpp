// Acceptance scenarios:
//   1) wall
//   2) crossing-boundary beams
//   3) repeated scans of the same surface
//   4) no-return beams (free-only, over max range)
// Each scenario compares against a beam-by-beam reference accumulation and
// checks numeric export contents.
#include "fusion.hpp"
#include "protocol.hpp"
#include "service.hpp"

#include "test_util.hpp"

#include <cmath>
#include <mutex>
#include <vector>

using namespace pf;

namespace {

MapConfig kCfg{20, 12, 0.5, -5.0, -3.0};

// Independent reference: accumulate beam updates in plain maps, replicating
// the documented rule (this is deliberately written independently of the
// grid traversal, using per-cell parametric interval math).
struct RefGrid {
    int w, h;
    double res, ox, oy;
    std::vector<double> lo;
    std::vector<unsigned char> obs;
    double lf, lh, lmax;

    RefGrid(const MapConfig& c, const FusionParams& p)
        : w(c.width), h(c.height), res(c.resolution), ox(c.origin_x),
          oy(c.origin_y), lf(p.l_free), lh(p.l_hit), lmax(p.l_max) {
        lo.assign(w * h, 0.0);
        obs.assign(w * h, 0);
    }

    // For every cell the beam's open segment enters, add lf; endpoint adds lh.
    // Implemented with fine parametric subdivision restricted to unique cells
    // (an intentionally different algorithm from the production DDA).
    void beam(const Beam& b) {
        double ex = b.ex, ey = b.ey;
        if (!b.hit) {
            double dx = b.ex - b.ox, dy = b.ey - b.oy;
            double len = std::hypot(dx, dy);
            ex = b.ox + dx / len * b.max_range;
            ey = b.oy + dy / len * b.max_range;
        }
        double dx = ex - b.ox, dy = ey - b.oy;
        double length = std::hypot(dx, dy);
        long long N = std::max<long long>(
            4000, static_cast<long long>(length / res * 4000));
        int eix = -1, eiy = -1;
        bool end_in = false;
        if (b.hit && length > 0) {
            eix = (int)std::floor((ex - ox) / res);
            eiy = (int)std::floor((ey - oy) / res);
            double x1 = ox + w * res, y1 = oy + h * res;
            end_in = ex > ox && ex < x1 && ey > oy && ey < y1 &&
                     eix >= 0 && eiy >= 0 && eix < w && eiy < h;
        }
        std::vector<char> touched_free(w * h, 0);
        for (long long k = 0; k < N; ++k) {
            double t = (double)k / N;
            double wx = b.ox + dx * t, wy = b.oy + dy * t;
            int ix = (int)std::floor((wx - ox) / res);
            int iy = (int)std::floor((wy - oy) / res);
            if (ix < 0 || iy < 0 || ix >= w || iy >= h) continue;
            if (end_in && ix == eix && iy == eiy) continue;
            int idx = iy * w + ix;
            if (!touched_free[idx]) {
                touched_free[idx] = 1;
                lo[idx] = std::max(-lmax, std::min(lmax, lo[idx] + lf));
                obs[idx] = 1;
            }
        }
        if (length == 0.0 && b.hit) {
            int ix = (int)std::floor((b.ox - ox) / res);
            int iy = (int)std::floor((b.oy - oy) / res);
            if (ix >= 0 && iy >= 0 && ix < w && iy < h) {
                int idx = iy * w + ix;
                lo[idx] = std::max(-lmax, std::min(lmax, lo[idx] + lh));
                obs[idx] = 1;
            }
        }
        if (end_in) {
            int idx = eiy * w + eix;
            lo[idx] = std::max(-lmax, std::min(lmax, lo[idx] + lh));
            obs[idx] = 1;
        }
    }
};

void compareWithRef(const OccupancyGrid& g, const RefGrid& ref) {
    for (int iy = 0; iy < kCfg.height; ++iy)
        for (int ix = 0; ix < kCfg.width; ++ix) {
            int idx = iy * kCfg.width + ix;
            CHECK_CLOSE(g.logOdds(ix, iy), ref.lo[idx], 1e-9);
            CHECK((ref.obs[idx] != 0) == g.observed(ix, iy));
        }
    CHECK(g.stateDigest() ==
          OccupancyGrid::stateDigestFrom(kCfg,
              GridSnapshot{ref.lo,
                           std::vector<std::uint8_t>(ref.obs.begin(),
                                                     ref.obs.end()),
                           {}, {}}));
}

struct Scenario {
    const char* name;
    std::vector<Beam> beams;
};

}  // namespace

static Scenario buildWallScenario() {
    // Sensor off any grid corner (grid lines are at multiples of 0.5
    // relative to origin -5.0), wall at x=3 spanning y in [-1,1].
    Scenario s;
    s.name = "wall";
    const double sx0 = 0.13, sy0 = 0.07;
    for (double a = -0.32; a <= 0.32 + 1e-9; a += 0.08) {
        double r = (3.0 - sx0) / std::cos(a);
        Beam b{sx0, sy0, sx0 + r * std::cos(a), sy0 + r * std::sin(a),
               true, 0};
        s.beams.push_back(b);
    }
    return s;
}

static Scenario buildBoundaryScenario() {
    // Rays that start outside and leave: endpoints beyond the AABB.
    Scenario s;
    s.name = "boundary";
    s.beams.push_back(Beam{-7.0, 0.13, 7.0, 0.13, true, 0});
    s.beams.push_back(Beam{0.13, -5.0, 0.13, 5.0, true, 0});
    s.beams.push_back(Beam{-7.0, 2.9, 7.0, -2.9, true, 0});
    s.beams.push_back(Beam{-7.0, 2.5, -6.5, 2.5, true, 0});  // never enters
    return s;
}

static Scenario buildRepeatScenario() {
    // Same scan from the same pose 8 times (pose placed off grid corners).
    Scenario s;
    s.name = "repeat";
    Pose pose{0.13, 0.07, 0.0};
    for (double a = -0.2; a <= 0.2 + 1e-9; a += 0.1) {
        SensorReturn r{a, 2.0};
        s.beams.push_back(beamFromReturn(pose, r, 6.0));
    }
    return s;
}

static Scenario buildNoReturnScenario() {
    Scenario s;
    s.name = "no_return";
    Pose pose{0.13, 0.07, 0.0};
    for (double a = -0.6; a <= 0.6 + 1e-9; a += 0.15) {
        SensorReturn r{a, -1.0};  // no return
        s.beams.push_back(beamFromReturn(pose, r, 4.0));
    }
    // And a genuine hit mixed in.
    s.beams.push_back(Beam{pose.x, pose.y, pose.x + 2.5, pose.y, true, 0});
    return s;
}

static void runScenario(const Scenario& sc, int repeats) {
    FusionParams p;
    OccupancyGrid g(kCfg, p);
    RefGrid ref(kCfg, p);
    ScanInput scan;
    scan.beams = sc.beams;
    for (int r = 0; r < repeats; ++r) {
        g.applyScan(scan);
        for (const Beam& b : sc.beams) ref.beam(b);
    }
    compareWithRef(g, ref);
}

static void scenario_wall() {
    runScenario(buildWallScenario(), 1);
}
static void scenario_boundary() {
    runScenario(buildBoundaryScenario(), 1);
    OccupancyGrid g(kCfg, FusionParams{});
    // Beam entirely missing the map touches nothing.
    UpdateCounts c = g.applyBeam(Beam{-7.0, 2.5, -6.5, 2.5, true, 0});
    CHECK(c.free_cells == 0 && c.occupied_cells == 0);
}
static void scenario_repeat() {
    runScenario(buildRepeatScenario(), 8);
    // Saturation: beam range 2.0 from pose (0.13,0.07) along +x.
    OccupancyGrid g(kCfg, FusionParams{});
    ScanInput scan;
    scan.beams = buildRepeatScenario().beams;
    for (int i = 0; i < 50; ++i) g.applyScan(scan);
    double ex = 0.13 + 2.0;
    int ix = (int)std::floor((ex - kCfg.origin_x) / kCfg.resolution);
    int iy = (int)std::floor((0.07 - kCfg.origin_y) / kCfg.resolution);
    CHECK_CLOSE(g.logOdds(ix, iy), FusionParams{}.l_max, 1e-9);
}
static void scenario_no_return() {
    runScenario(buildNoReturnScenario(), 1);
    // No-return beams create zero occupied cells on their own.
    OccupancyGrid g(kCfg, FusionParams{});
    Pose pose{0.13, 0.07, 0};
    int occupied = 0;
    for (double a = -0.6; a <= 0.6; a += 0.05) {
        Beam b = beamFromReturn(pose, SensorReturn{a, -1.0}, 4.0);
        occupied += static_cast<int>(g.applyBeam(b).occupied_cells);
    }
    CHECK(occupied == 0);
    // Cells beyond max range remain unknown.
    int far_ix = (int)std::floor((4.6 - kCfg.origin_x) / kCfg.resolution);
    int far_iy = (int)std::floor((0.07 - kCfg.origin_y) / kCfg.resolution);
    CHECK(!g.observed(far_ix, far_iy));
}

static void versioned_export_matches_state() {
    MapService svc;
    CreateMapOptions opt;
    opt.width = kCfg.width;
    opt.height = kCfg.height;
    opt.resolution = kCfg.resolution;
    opt.origin_x = kCfg.origin_x;
    opt.origin_y = kCfg.origin_y;
    MapRecord& rec = svc.createMap(opt, 5000);
    std::lock_guard<std::mutex> lock(rec.mu);

    ScanInput scan;
    scan.beams = buildWallScenario().beams;
    GridVersion v = applyScanVersioned(rec, scan, 5001, 0, false);

    Json j = gridExportJson(rec, v);
    CHECK(j["seq"] == 1);
    CHECK(j["config"]["resolution"] == kCfg.resolution);
    int observed = 0;
    int unknown = 0;
    for (const auto& row : j["grid"]["cells"])
        for (const auto& cell : row) {
            if (cell["state"] == "observed") {
                ++observed;
                CHECK(cell["p"].is_number());
                double pv = cell["p"].get<double>();
                CHECK(pv > 0.0 && pv < 1.0);
            } else {
                ++unknown;
                CHECK(cell["p"].is_null());
            }
        }
    CHECK(observed > 0);
    CHECK(observed + unknown == kCfg.width * kCfg.height);
    CHECK(j["state_digest"] == v.state_digest);
}

int main() {
    RUN_TEST(scenario_wall);
    RUN_TEST(scenario_boundary);
    RUN_TEST(scenario_repeat);
    RUN_TEST(scenario_no_return);
    RUN_TEST(versioned_export_matches_state);
    return TEST_MAIN_RETURN();
}
