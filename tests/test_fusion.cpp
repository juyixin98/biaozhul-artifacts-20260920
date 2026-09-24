#include "fusion.hpp"
#include "service.hpp"

#include "test_util.hpp"

#include <cmath>
#include <mutex>
#include <stdexcept>

using namespace pf;

namespace {

MapRecord& newMap(MapService& svc) {
    CreateMapOptions opt;
    opt.width = 10;
    opt.height = 6;
    opt.resolution = 0.5;
    opt.origin_x = -2.0;
    opt.origin_y = -1.0;
    return svc.createMap(opt, 1000);
}

ScanInput scanWith(std::vector<Beam> beams, Pose pose = {}) {
    ScanInput s;
    s.pose = pose;
    s.beams = std::move(beams);
    return s;
}

}  // namespace

static void empty_version_v0() {
    MapService svc;
    MapRecord& rec = newMap(svc);
    std::lock_guard<std::mutex> lock(rec.mu);
    CHECK(rec.versions.size() == 1);
    CHECK(rec.versions[0].seq == 0);
    CHECK(rec.versions[0].version_digest.size() == 64);
    CHECK(rec.versions[0].state_digest.size() == 64);
    for (int k = 0; k < 10 * 6; ++k) {
        CHECK(rec.versions[0].snapshot.observed[k] == 0);
        CHECK(rec.versions[0].snapshot.log_odds[k] == 0.0);
    }
    // sha256 of empty payload
    CHECK(rec.versions[0].payload_digest ==
          crypto::sha256_hex(""));
}

static void batched_equals_beam_by_beam_reference() {
    MapConfig cfg{12, 9, 0.5, -1.0, -0.75};
    FusionParams params;

    // Reference: apply each beam to its own fresh-then-accumulated grid.
    OccupancyGrid ref(cfg, params);
    OccupancyGrid batched(cfg, params);

    std::vector<Beam> beams;
    double angles[] = {-0.9, -0.45, 0.0, 0.2, 0.55, 0.95};
    Pose pose{1.2, 0.3, 0.1};
    for (double a : angles) {
        SensorReturn r{a, 1.7 + std::fabs(a)};
        beams.push_back(beamFromReturn(pose, r, 2.5));
        SensorReturn miss{a + 0.07, -1.0};
        beams.push_back(beamFromReturn(pose, miss, 2.5));
    }
    for (const Beam& b : beams) ref.applyBeam(b);
    ScanInput scan;
    scan.pose = pose;
    scan.beams = beams;
    batched.applyScan(scan);

    CHECK(ref.stateDigest() == batched.stateDigest());
    for (int iy = 0; iy < cfg.height; ++iy)
        for (int ix = 0; ix < cfg.width; ++ix) {
            CHECK_CLOSE(ref.logOdds(ix, iy), batched.logOdds(ix, iy), 1e-15);
            CHECK(ref.observed(ix, iy) == batched.observed(ix, iy));
        }
}

static void repeated_scans_accumulate_and_saturate() {
    MapService svc;
    MapRecord& rec = newMap(svc);
    std::lock_guard<std::mutex> lock(rec.mu);
    Beam b{0.0, 0.5, 2.5, 0.5, true, 0};  // world coords inside map
    ScanInput s = scanWith({b});
    FusionParams fp = rec.params;
    for (int i = 1; i <= 20; ++i) {
        GridVersion v = applyScanVersioned(rec, s, 1000 + i, 0, false);
        CHECK(v.seq == static_cast<std::uint64_t>(i));
    }
    // Endpoint cell (world (2.5,0.5) => ix=(2.5+2)/0.5=9, iy=(0.5+1)/0.5=3)
    int ix = 9, iy = 3;
    CHECK(rec.grid->hitCount(ix, iy) == 20);
    CHECK(rec.grid->logOdds(ix, iy) <= fp.l_max + 1e-12);
    CHECK_CLOSE(rec.grid->logOdds(ix, iy), fp.l_max, 1e-9);
    CHECK(rec.grid->probability(ix, iy) > 0.95);
    // Same payload repeats => same payload digest, but chain digest changes.
    CHECK(rec.versions[1].payload_digest == rec.versions[2].payload_digest);
    CHECK(rec.versions[1].state_digest != rec.versions[2].state_digest);
    CHECK(rec.versions[1].version_digest != rec.versions[2].version_digest);
    CHECK(rec.versions.back().version_digest != rec.versions.front().version_digest);
}

static void observed_at_prior_distinct_from_unknown() {
    MapConfig cfg{3, 1, 1.0, 0.0, 0.0};
    OccupancyGrid g(cfg, FusionParams{});
    // Free then hit the same cell -> log-odds returns to 0, but it IS observed.
    Beam f{0.5, 0.5, 1.5, 0.5, true, 0};  // cell0 free, cell1 hit
    Beam h{1.5, 0.5, 0.5, 0.5, true, 0};  // cell1 free, cell0 hit
    g.applyBeam(f);
    g.applyBeam(h);
    CHECK(std::fabs(g.logOdds(0, 0)) < 1e-12);
    CHECK(g.observed(0, 0));
    CHECK_CLOSE(g.probability(0, 0), 0.5, 1e-12);
    CHECK(!g.observed(2, 0));  // never touched => unknown, also p=0.5
}

static void version_chain_recomputes() {
    MapService svc;
    MapRecord& rec = newMap(svc);
    std::lock_guard<std::mutex> lock(rec.mu);
    for (int i = 1; i <= 3; ++i) {
        applyScanVersioned(rec,
                           scanWith({{-1.0 + i * 0.2, 0.5, 2.0, 0.5, true, 0}}),
                           2000 + i, 0, false);
    }
    std::string parent(64, '0');
    for (const GridVersion& v : rec.versions) {
        std::string state =
            OccupancyGrid::stateDigestFrom(rec.config, v.snapshot);
        std::string ver =
            chainDigest(v.seq, parent, v.payload_digest, state);
        CHECK(state == v.state_digest);
        CHECK(ver == v.version_digest);
        parent = v.version_digest;
    }
}

static void expected_seq_optimistic_concurrency() {
    MapService svc;
    MapRecord& rec = newMap(svc);
    std::lock_guard<std::mutex> lock(rec.mu);
    applyScanVersioned(rec, scanWith({{0.5, 0.5, 1.5, 0.5, true, 0}}),
                       3000, 0, true);  // base 0 ok
    bool threw = false;
    try {
        // Client still thinks it is based at 0, but current seq is 1.
        applyScanVersioned(rec, scanWith({{0.5, 0.5, 1.5, 0.5, true, 0}}),
                           3001, 0, true);
    } catch (const std::invalid_argument&) {
        threw = true;
    }
    CHECK(threw);
    CHECK(rec.versions.size() == 2);  // nothing appended
}

static void invalid_geometry_rejected() {
    MapService svc;
    MapRecord& rec = newMap(svc);
    std::lock_guard<std::mutex> lock(rec.mu);
    bool threw = false;
    try {
        Beam bad{0.5, 0.5, std::nan(""), 0.5, true, 0};
        applyScanVersioned(rec, scanWith({bad}), 4000, 0, false);
    } catch (const std::invalid_argument&) {
        threw = true;
    }
    CHECK(threw);

    threw = false;
    try {
        Beam bad{0.5, 0.5, 1.5, 0.5, false, 0.0};
        applyScanVersioned(rec, scanWith({bad}), 4001, 0, false);
    } catch (const std::invalid_argument&) {
        threw = true;
    }
    CHECK(threw);
}

int main() {
    RUN_TEST(empty_version_v0);
    RUN_TEST(batched_equals_beam_by_beam_reference);
    RUN_TEST(repeated_scans_accumulate_and_saturate);
    RUN_TEST(observed_at_prior_distinct_from_unknown);
    RUN_TEST(version_chain_recomputes);
    RUN_TEST(expected_seq_optimistic_concurrency);
    RUN_TEST(invalid_geometry_rejected);
    return TEST_MAIN_RETURN();
}
