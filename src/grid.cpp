#include "grid.hpp"

#include "crypto.hpp"
#include "format.hpp"

#include <algorithm>
#include <cmath>
#include <limits>
#include <sstream>
#include <stdexcept>

namespace pf {

namespace {

constexpr double kInf = std::numeric_limits<double>::infinity();
// Epsilon expressed in cell sizes; avoids boundary ambiguity in traversal.
constexpr double kGridEps = 1e-9;

// Quantize a log-odds value to integer micro-units used by the canonical
// state digest. Integer quantization keeps the digest well-defined across
// languages (C++/Python) despite differing float formatting.
long long microLog(double l) {
    // floor(x + 0.5), reproduced identically by the Python reference checker.
    return static_cast<long long>(std::floor(l * 1e6 + 0.5));
}

}  // namespace

double OccupancyGrid::logit(double p) {
    if (!(p > 0.0 && p < 1.0))
        throw std::invalid_argument("probability must be in (0,1)");
    return std::log(p / (1.0 - p));
}

double OccupancyGrid::sigmoid(double l) {
    return 1.0 / (1.0 + std::exp(-l));
}

OccupancyGrid::OccupancyGrid(MapConfig cfg, FusionParams params)
    : cfg_(cfg), params_(params) {
    if (cfg_.width <= 0 || cfg_.height <= 0)
        throw std::invalid_argument("map width/height must be positive");
    if (!(cfg_.resolution > 0.0))
        throw std::invalid_argument("resolution must be positive");
    if (!(params_.l_max > 0.0))
        throw std::invalid_argument("l_max must be positive");
    const int n = cfg_.width * cfg_.height;
    lo_.setZero(n);
    obs_.setZero(n);
    hit_n_.setZero(n);
    free_n_.setZero(n);
}

void OccupancyGrid::worldToCell(double wx, double wy, int& ix, int& iy) const {
    ix = static_cast<int>(std::floor((wx - cfg_.origin_x) / cfg_.resolution));
    iy = static_cast<int>(std::floor((wy - cfg_.origin_y) / cfg_.resolution));
}

double OccupancyGrid::probability(int ix, int iy) const {
    return sigmoid(lo_(index(ix, iy)));
}

void OccupancyGrid::touchFree(int cell) {
    double v = lo_[cell] + params_.l_free;
    lo_[cell] = std::clamp(v, -params_.l_max, params_.l_max);
    obs_[cell] = 1;
    ++free_n_[cell];
}

void OccupancyGrid::touchHit(int cell) {
    double v = lo_[cell] + params_.l_hit;
    lo_[cell] = std::clamp(v, -params_.l_max, params_.l_max);
    obs_[cell] = 1;
    ++hit_n_[cell];
}

UpdateCounts OccupancyGrid::applyBeam(const Beam& b) {
    return castBeam(b);
}

UpdateCounts OccupancyGrid::applyScan(const ScanInput& scan) {
    UpdateCounts total;
    for (const Beam& b : scan.beams) {
        UpdateCounts c = castBeam(b);
        total.free_cells += c.free_cells;
        total.occupied_cells += c.occupied_cells;
    }
    return total;
}

// ---------------------------------------------------------------------------
// Ray/grid clipping + Amanatides-Woo traversal, expressed in physical
// distance s along the (unit) ray direction.
//
//   * The segment is clipped to the grid AABB via slab intersection.
//   * Cells crossed by the OPEN segment are free; for a real hit whose
//     endpoint is strictly inside the grid, the endpoint cell is occupied
//     and is not also touched as free.
//   * A ray leaving the convex grid cannot re-enter (no voxel visiting after
//     exit).
//   * An endpoint exactly on the grid border is outside by convention, so a
//     hit there produces free cells only.
//   * A crossing exactly at a cell corner steps both axes simultaneously;
//     the two diagonally side-adjacent cells are not visited.
// ---------------------------------------------------------------------------
UpdateCounts OccupancyGrid::castBeam(const Beam& b) {
    UpdateCounts counts;

    // Build the segment origin->end. For a no-return beam the caller provides
    // a direction hint through (ex,ey); the actual virtual end is extended to
    // max_range along that direction.
    double ex = b.ex;
    double ey = b.ey;
    if (!b.hit) {
        double hdx = b.ex - b.ox;
        double hdy = b.ey - b.oy;
        double hlen = std::hypot(hdx, hdy);
        if (!(hlen > 0.0) || !(b.max_range > 0.0)) return counts;
        ex = b.ox + hdx / hlen * b.max_range;
        ey = b.oy + hdy / hlen * b.max_range;
    }

    double dx = ex - b.ox;
    double dy = ey - b.oy;
    double ray_len = std::hypot(dx, dy);
    if (!(ray_len > 0.0)) {
        if (b.hit) {
            int ix, iy;
            worldToCell(b.ox, b.oy, ix, iy);
            if (inside(ix, iy)) {
                touchHit(index(ix, iy));
                counts.occupied_cells = 1;
            }
        }
        return counts;
    }
    double ux = dx / ray_len;
    double uy = dy / ray_len;

    const double res = cfg_.resolution;
    const double x0 = cfg_.origin_x;
    const double y0 = cfg_.origin_y;
    const double x1 = x0 + cfg_.width * res;
    const double y1 = y0 + cfg_.height * res;

    // Slab intersection in t in [0,1] along (dx,dy).
    double t_near = 0.0;
    double t_far = 1.0;
    auto clip_slab = [&](double o, double d, double lo, double hi) -> bool {
        if (std::abs(d) < 1e-15) {
            return o >= lo && o <= hi;
        }
        double ta = (lo - o) / d;
        double tb = (hi - o) / d;
        if (ta > tb) std::swap(ta, tb);
        t_near = std::max(t_near, ta);
        t_far = std::min(t_far, tb);
        return t_near <= t_far + 1e-12;
    };
    if (!clip_slab(b.ox, dx, x0, x1) || !clip_slab(b.oy, dy, y0, y1) ||
        t_far < 0.0 || t_near > 1.0) {
        return counts;  // misses grid entirely
    }
    t_near = std::clamp(t_near, 0.0, 1.0);
    t_far = std::clamp(t_far, 0.0, 1.0);

    bool endpoint_strictly_inside =
        b.hit && ex > x0 && ex < x1 && ey > y0 && ey < y1;

    int end_ix = -1;
    int end_iy = -1;
    if (endpoint_strictly_inside) {
        worldToCell(ex, ey, end_ix, end_iy);
        if (!inside(end_ix, end_iy)) endpoint_strictly_inside = false;
    }

    // Entry point and starting cell.
    double px = b.ox + dx * t_near;
    double py = b.oy + dy * t_near;
    // Nudge epsilon inward so a point exactly on a grid line classifies into
    // the cell the ray actually enters (consistent tie-break with traversal).
    px += ux * res * 1e-12;
    py += uy * res * 1e-12;
    int ix = std::clamp(static_cast<int>(std::floor((px - x0) / res)),
                        0, cfg_.width - 1);
    int iy = std::clamp(static_cast<int>(std::floor((py - y0) / res)),
                        0, cfg_.height - 1);

    int sx = ux > 1e-15 ? 1 : (ux < -1e-15 ? -1 : 0);
    int sy = uy > 1e-15 ? 1 : (uy < -1e-15 ? -1 : 0);

    // Physical distance along ray from the current point to the next
    // vertical (tmx) / horizontal (tmy) grid boundary.
    auto dist_to_boundary = [&](int cell, int step, double p,
                                double origin) -> double {
        if (step == 0) return kInf;
        double boundary_w = step > 0 ? origin + (cell + 1) * res
                                     : origin + cell * res;
        double delta = boundary_w - p;  // sign matches step*dir
        double ucomp = step > 0 ? (p == px ? ux : uy) : (p == px ? ux : uy);
        (void)ucomp;
        return step > 0 ? delta / ux : delta / ux;
    };
    (void)dist_to_boundary;

    double tmx = kInf;
    double tmy = kInf;
    if (sx != 0) {
        double bx = sx > 0 ? x0 + (ix + 1) * res : x0 + ix * res;
        tmx = (bx - px) / ux;  // both numerator and ux share the sign
    }
    if (sy != 0) {
        double by = sy > 0 ? y0 + (iy + 1) * res : y0 + iy * res;
        tmy = (by - py) / uy;
    }
    double tdx = sx != 0 ? res / std::abs(ux) : kInf;
    double tdy = sy != 0 ? res / std::abs(uy) : kInf;

    // Physical distance to the segment's exit from the grid.
    double dist_total = (t_far - t_near) * ray_len;

    const int max_steps = cfg_.width + cfg_.height + 4;
    const double eps = kGridEps * res;

    for (int steps = 0; steps <= max_steps; ++steps) {
        if (!inside(ix, iy)) break;

        bool is_endpoint_cell = endpoint_strictly_inside &&
                                ix == end_ix && iy == end_iy;
        if (!is_endpoint_cell) {
            touchFree(index(ix, iy));
            ++counts.free_cells;
        }

        double advance = std::min(tmx, tmy);
        if (advance >= dist_total - eps) break;  // reached exit/endpoint

        tmx -= advance;
        tmy -= advance;
        dist_total -= advance;
        if (tmx <= eps && tmy <= eps) {
            ix += sx;
            iy += sy;
            tmx = tdx;
            tmy = tdy;
        } else if (tmx <= eps) {
            ix += sx;
            tmx = tdx;
        } else {
            iy += sy;
            tmy = tdy;
        }
    }

    if (endpoint_strictly_inside) {
        touchHit(index(end_ix, end_iy));
        ++counts.occupied_cells;
    }
    return counts;
}

GridSnapshot OccupancyGrid::snapshot() const {
    GridSnapshot s;
    s.log_odds.assign(lo_.data(), lo_.data() + lo_.size());
    s.observed.assign(obs_.data(), obs_.data() + obs_.size());
    s.free_touches.assign(free_n_.data(), free_n_.data() + free_n_.size());
    s.hit_touches.assign(hit_n_.data(), hit_n_.data() + hit_n_.size());
    return s;
}

std::string OccupancyGrid::configCanonical(const MapConfig& cfg,
                                           const FusionParams& p) {
    std::ostringstream os;
    os << "GRID-CONFIG:v1\n";
    os << "resolution=" << formatDouble(cfg.resolution) << '\n';
    os << "origin_x=" << formatDouble(cfg.origin_x) << '\n';
    os << "origin_y=" << formatDouble(cfg.origin_y) << '\n';
    os << "width=" << cfg.width << '\n';
    os << "height=" << cfg.height << '\n';
    os << "l_free=" << formatDouble(p.l_free) << '\n';
    os << "l_hit=" << formatDouble(p.l_hit) << '\n';
    os << "l_max=" << formatDouble(p.l_max) << '\n';
    return os.str();
}

std::string OccupancyGrid::stateDigestFrom(const MapConfig& cfg,
                                           const GridSnapshot& snap) {
    std::ostringstream os;
    os << "GRID-STATE:v1\n";
    os << "resolution=" << formatDouble(cfg.resolution) << '\n';
    os << "origin_x=" << formatDouble(cfg.origin_x) << '\n';
    os << "origin_y=" << formatDouble(cfg.origin_y) << '\n';
    os << "width=" << cfg.width << '\n';
    os << "height=" << cfg.height << '\n';
    const int n = cfg.width * cfg.height;
    for (int k = 0; k < n; ++k) {
        int iy = k / cfg.width;
        int ix = k - iy * cfg.width;
        os << ix << ',' << iy << ',' << microLog(snap.log_odds[k]) << ','
           << static_cast<int>(snap.observed[k]) << '\n';
    }
    return crypto::sha256_hex(os.str());
}

std::string OccupancyGrid::stateDigest() const {
    return stateDigestFrom(cfg_, snapshot());
}

}  // namespace pf
