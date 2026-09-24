#include "crypto.hpp"
#include "grid.hpp"

#include "test_util.hpp"

#include <cmath>

using namespace pf;

static void logit_sigmoid_inverse() {
    for (double p : {0.01, 0.1, 0.4, 0.5, 0.6, 0.9, 0.99}) {
        CHECK_CLOSE(OccupancyGrid::sigmoid(OccupancyGrid::logit(p)), p, 1e-12);
    }
    // Prior p=0.5 => log-odds 0.
    CHECK_CLOSE(OccupancyGrid::logit(0.5), 0.0, 1e-15);
    CHECK_CLOSE(OccupancyGrid::sigmoid(0.0), 0.5, 1e-15);
    // Defaults: logit(0.4) = -ln1.5, logit(0.6) = ln1.5.
    FusionParams d;
    CHECK_CLOSE(d.l_free, std::log(2.0 / 3.0), 1e-12);
    CHECK_CLOSE(OccupancyGrid::logit(0.6), std::log(1.5), 1e-12);
}

static void saturation_bounds() {
    MapConfig cfg{4, 4, 1.0, 0.0, 0.0};
    FusionParams p;
    OccupancyGrid g(cfg, p);
    Beam hit{1.5, 1.5, 1.5, 1.5, true, 0};  // zero-length: end cell occupied
    for (int i = 0; i < 100; ++i) g.applyBeam(hit);
    double lo = g.logOdds(1, 1);
    CHECK(lo <= p.l_max + 1e-12);
    CHECK_CLOSE(lo, 3.0, 1e-12);
    CHECK(g.probability(1, 1) > 0.95);
}

static void saturation_lower_bound() {
    MapConfig cfg{4, 4, 1.0, 0.0, 0.0};
    Beam noreturn{1.5, 1.5, 1.5, 2.5, false, 1.0};  // +y, free only
    OccupancyGrid g(cfg, FusionParams{});
    for (int i = 0; i < 100; ++i) g.applyBeam(noreturn);
    for (int iy = 1; iy <= 2; ++iy) {
        CHECK(g.logOdds(1, iy) >= -3.0 - 1e-12);
    }
    CHECK_CLOSE(g.logOdds(1, 1), -3.0, 1e-12);
}

int main() {
    RUN_TEST(logit_sigmoid_inverse);
    RUN_TEST(saturation_bounds);
    RUN_TEST(saturation_lower_bound);
    return TEST_MAIN_RETURN();
}
