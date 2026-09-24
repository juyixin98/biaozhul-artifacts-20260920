#include "grid.hpp"

#include "test_util.hpp"

#include <array>
#include <cmath>
#include <random>
#include <set>
#include <utility>
#include <vector>

using namespace pf;
using Cell = std::pair<int, int>;

namespace {

MapConfig kCfg{6, 3, 1.0, 0.0, 0.0};

// Which cells did the grid mark free / occupied after one beam?
std::set<Cell> touchedMask(const OccupancyGrid& g, bool want_hit) {
    std::set<Cell> out;
    for (int iy = 0; iy < g.config().height; ++iy)
        for (int ix = 0; ix < g.config().width; ++ix) {
            if (want_hit ? g.hitCount(ix, iy) > 0 : g.freeCount(ix, iy) > 0)
                out.insert({ix, iy});
        }
    return out;
}

// Independent dense-supersampling reference of the documented ray rules.
// The segment is sampled at ~8000 points per cell; cells of open-segment
// points are free, the strict-inside endpoint cell is occupied (hit only).
struct RefResult {
    std::set<Cell> free_cells;
    std::set<Cell> hit_cells;
};

RefResult referenceCast(const MapConfig& cfg, const Beam& b) {
    RefResult r;
    double ex = b.ex, ey = b.ey;
    if (!b.hit) {
        double dx = b.ex - b.ox, dy = b.ey - b.oy;
        double len = std::hypot(dx, dy);
        ex = b.ox + dx / len * b.max_range;
        ey = b.oy + dy / len * b.max_range;
    }
    double dx = ex - b.ox, dy = ey - b.oy;
    double length = std::hypot(dx, dy);
    long long N = std::max<long long>(20000,
                                      static_cast<long long>(length / cfg.resolution * 8000));
    auto cell_of = [&](double t, int& ix, int& iy) {
        double wx = b.ox + dx * t;
        double wy = b.oy + dy * t;
        ix = static_cast<int>(std::floor((wx - cfg.origin_x) / cfg.resolution));
        iy = static_cast<int>(std::floor((wy - cfg.origin_y) / cfg.resolution));
    };
    int eix = 0, eiy = 0;
    cell_of(1.0, eix, eiy);
    bool end_inside = b.hit && eix >= 0 && eiy >= 0 &&
                      eix < cfg.width && eiy < cfg.height;
    // Exact border check.
    if (b.hit) {
        double x1 = cfg.origin_x + cfg.width * cfg.resolution;
        double y1 = cfg.origin_y + cfg.height * cfg.resolution;
        if (!(ex > cfg.origin_x && ex < x1 && ey > cfg.origin_y && ey < y1))
            end_inside = false;
    }
    // Origin cell is part of the open segment too.
    for (long long k = 0; k < N; ++k) {
        double t = static_cast<double>(k) / N;  // [0,1)
        int ix, iy;
        cell_of(t, ix, iy);
        if (ix < 0 || iy < 0 || ix >= cfg.width || iy >= cfg.height) continue;
        if (end_inside && ix == eix && iy == eiy) continue;
        r.free_cells.insert({ix, iy});
    }
    if (end_inside) r.hit_cells.insert({eix, eiy});
    return r;
}

void expectBeam(const MapConfig& cfg, const Beam& b, const char* name) {
    OccupancyGrid g(cfg, FusionParams{});
    UpdateCounts c = g.applyBeam(b);
    auto f = touchedMask(g, false);
    auto h = touchedMask(g, true);
    RefResult ref = referenceCast(cfg, b);
    pf_test::report(f == ref.free_cells, name, __FILE__, __LINE__,
                    std::string(" free: got ") + std::to_string(f.size()) +
                        " ref " + std::to_string(ref.free_cells.size()));
    pf_test::report(h == ref.hit_cells, name, __FILE__, __LINE__,
                    std::string(" hit: got ") + std::to_string(h.size()) +
                        " ref " + std::to_string(ref.hit_cells.size()));
    pf_test::report(c.free_cells == f.size() &&
                        c.occupied_cells == h.size(),
                    name, __FILE__, __LINE__, " count mismatch");
}

}  // namespace

static void horizontal_hit() {
    Beam b{0.5, 1.5, 4.5, 1.5, true, 0};
    OccupancyGrid g(kCfg, FusionParams{});
    g.applyBeam(b);
    for (int ix = 0; ix <= 3; ++ix) {
        CHECK(g.freeCount(ix, 1) == 1);
        CHECK(g.hitCount(ix, 1) == 0);
    }
    CHECK(g.hitCount(4, 1) == 1);
    CHECK(g.freeCount(4, 1) == 0);
    // Rows 0 and 2 untouched (unknown).
    for (int ix = 0; ix < 6; ++ix) {
        CHECK(!g.observed(ix, 0));
        CHECK(!g.observed(ix, 2));
    }
}

static void no_return_free_only() {
    Beam b{0.5, 1.5, 4.5, 1.5, false, 4.0};  // virtual end (4.5,1.5)
    OccupancyGrid g(kCfg, FusionParams{});
    UpdateCounts c = g.applyBeam(b);
    CHECK(c.occupied_cells == 0);
    CHECK(c.free_cells == 5);
    for (int ix = 0; ix <= 4; ++ix) CHECK(g.freeCount(ix, 1) == 1);
}

static void crossing_boundary_both_sides() {
    // Origin outside left, endpoint outside right: free only inside grid.
    Beam b{-1.5, 1.5, 6.5, 1.5, true, 0};
    OccupancyGrid g(kCfg, FusionParams{});
    UpdateCounts c = g.applyBeam(b);
    CHECK(c.occupied_cells == 0);
    CHECK(c.free_cells == 6);
    for (int ix = 0; ix < 6; ++ix) CHECK(g.freeCount(ix, 1) == 1);
}

static void endpoint_on_border_is_not_occupied() {
    Beam b{0.5, 1.5, 6.0, 1.5, true, 0};  // endpoint exactly at x1
    OccupancyGrid g(kCfg, FusionParams{});
    UpdateCounts c = g.applyBeam(b);
    CHECK(c.occupied_cells == 0);
    CHECK(c.free_cells == 6);
}

static void wall_from_both_sides() {
    Beam left{0.5, 1.5, 3.45, 1.5, true, 0};
    Beam right{4.5, 1.5, 3.55, 1.5, true, 0};  // travels -x
    OccupancyGrid g(kCfg, FusionParams{});
    g.applyBeam(left);
    g.applyBeam(right);
    FusionParams p;
    CHECK_CLOSE(g.logOdds(3, 1), 2 * p.l_hit, 1e-12);  // wall cell
    CHECK(g.hitCount(3, 1) == 2);
    CHECK(g.freeCount(3, 1) == 0);                    // never marked free
    for (int ix = 0; ix <= 2; ++ix) CHECK(g.freeCount(ix, 1) == 1);
    CHECK(g.freeCount(4, 1) == 1);
}

static void diagonal_corner_convention() {
    MapConfig cfg{5, 5, 1.0, 0.0, 0.0};
    Beam b{0.5, 0.5, 3.5, 3.5, true, 0};
    OccupancyGrid g(cfg, FusionParams{});
    g.applyBeam(b);
    // Diagonal cells only; the side-adjacent cells at each corner are skipped.
    for (int k = 0; k <= 2; ++k) {
        CHECK(g.freeCount(k, k) == 1);
        CHECK(g.hitCount(k, k) == 0);
    }
    CHECK(g.hitCount(3, 3) == 1);
    static const std::array<Cell, 6> sides = {
        Cell{1, 0}, {0, 1}, {2, 1}, {1, 2}, {3, 2}, {2, 3}};
    for (auto [ix, iy] : sides) CHECK(!g.observed(ix, iy));
}

static void reference_fuzz() {
    std::mt19937_64 rng(12345);
    std::uniform_real_distribution<double> u(-3.0, 9.0);
    std::uniform_real_distribution<double> ang(-M_PI, M_PI);
    std::uniform_real_distribution<double> rng_range(0.2, 6.0);
    MapConfig cfg{12, 9, 0.5, -1.0, -0.75};
    for (int trial = 0; trial < 200; ++trial) {
        Beam b;
        b.ox = u(rng);
        b.oy = u(rng) * 0.75;
        double a = ang(rng);
        double r = rng_range(rng);
        bool hit = (trial % 3) != 0;
        b.max_range = 5.0;
        if (hit) {
            b.hit = true;
            b.ex = b.ox + r * std::cos(a);
            b.ey = b.oy + r * std::sin(a);
        } else {
            b.hit = false;
            b.ex = b.ox + std::cos(a);  // direction hint
            b.ey = b.oy + std::sin(a);
        }
        expectBeam(cfg, b, "fuzz beam");
    }
}

static void origin_cell_is_free() {
    Beam b{0.2, 0.2, 3.8, 0.2, true, 0};
    OccupancyGrid g(MapConfig{4, 1, 1.0, 0.0, 0.0}, FusionParams{});
    g.applyBeam(b);
    CHECK(g.freeCount(0, 0) == 1);
    CHECK(g.freeCount(1, 0) == 1);
    CHECK(g.freeCount(2, 0) == 1);
    CHECK(g.hitCount(2, 0) == 0);
    CHECK(g.hitCount(3, 0) == 1);
}

int main() {
    RUN_TEST(horizontal_hit);
    RUN_TEST(no_return_free_only);
    RUN_TEST(crossing_boundary_both_sides);
    RUN_TEST(endpoint_on_border_is_not_occupied);
    RUN_TEST(wall_from_both_sides);
    RUN_TEST(diagonal_corner_convention);
    RUN_TEST(origin_cell_is_free);
    RUN_TEST(reference_fuzz);
    return TEST_MAIN_RETURN();
}
