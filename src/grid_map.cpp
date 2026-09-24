#include "gridfusion/grid_map.hpp"

#include <algorithm>
#include <cmath>
#include <limits>
#include <sstream>

#include <Eigen/Dense>

#include "gridfusion/sha256.hpp"

namespace gridfusion {

namespace {
bool isFinite(double v) { return std::isfinite(v); }
}  // namespace

std::string MapConfig::validate() const {
    if (!(isFinite(resolution) && resolution > 0.0))
        return "resolution must be a finite number > 0";
    if (!(width > 0 && height > 0))
        return "width and height must be positive integers";
    if (!isFinite(origin_x) || !isFinite(origin_y))
        return "origin must be finite";
    const double span_x = resolution * width;
    const double span_y = resolution * height;
    if (!isFinite(span_x) || !isFinite(span_y) || span_x > 1e7 ||
        span_y > 1e7)
        return "map span must be finite and below 1e7 metres";
    if (!isFinite(l_occ) || !isFinite(l_free) || !isFinite(l_min) ||
        !isFinite(l_max))
        return "log-odds parameters must be finite";
    if (!(l_min < 0.0 && l_max > 0.0))
        return "l_min must be < 0 and l_max > 0";
    if (!(l_occ > 0.0)) return "l_occ must be > 0";
    if (!(l_free < 0.0)) return "l_free must be < 0";
    if (!(default_max_range > 0.0 && isFinite(default_max_range)))
        return "default_max_range must be > 0";
    return "";
}

bool MapConfig::same_geometry(const MapConfig& o) const {
    return resolution == o.resolution && origin_x == o.origin_x &&
           origin_y == o.origin_y && width == o.width && height == o.height;
}

bool MapConfig::operator==(const MapConfig& o) const {
    return same_geometry(o) && l_occ == o.l_occ && l_free == o.l_free &&
           l_min == o.l_min && l_max == o.l_max &&
           default_max_range == o.default_max_range;
}

double logitFromProbability(double p) {
    if (p <= 0.0) return -std::numeric_limits<double>::infinity();
    if (p >= 1.0) return std::numeric_limits<double>::infinity();
    return std::log(p / (1.0 - p));
}

std::string GridMap::canonical_geometry(const MapConfig& cfg) {
    // 17 significant digits round-trips an IEEE-754 double exactly, so the
    // same numeric geometry always yields the same hash regardless of how
    // the JSON text happened to spell it.
    std::ostringstream os;
    os.precision(17);
    os << cfg.resolution << '|' << cfg.origin_x << '|' << cfg.origin_y << '|'
       << cfg.width << '|' << cfg.height;
    return os.str();
}

std::string GridMap::make_version_id(const MapConfig& cfg) {
    return sha256::hex(canonical_geometry(cfg));
}

GridMap::GridMap(const MapConfig& cfg) : cfg_(cfg) {
    if (!cfg_.validate().empty())
        throw std::invalid_argument(cfg_.validate());
    version_id_ = make_version_id(cfg_);
    cells_.resize(static_cast<std::size_t>(cfg_.width) * cfg_.height);
}

void GridMap::reset() {
    for (Cell& c : cells_) c = Cell{};
}

int GridMap::worldToCellX(double x) const {
    return static_cast<int>(std::floor((x - cfg_.origin_x) /
                                       cfg_.resolution));
}
int GridMap::worldToCellY(double y) const {
    return static_cast<int>(std::floor((y - cfg_.origin_y) /
                                       cfg_.resolution));
}

bool GridMap::poseInside(const Pose& p) const {
    if (!isFinite(p.x) || !isFinite(p.y) || !isFinite(p.theta)) return false;
    const int cx = worldToCellX(p.x);
    const int cy = worldToCellY(p.y);
    return cx >= 0 && cx < cfg_.width && cy >= 0 && cy < cfg_.height;
}

void GridMap::applyBeam(const PreparedBeam& b, TraversalFn traverse,
                        ScanStats& stats) {
    bool clipped = traverse(
        b.sx, b.sy, b.dx, b.dy, b.travel, cfg_.width, cfg_.height,
        cfg_.origin_x, cfg_.origin_y, cfg_.resolution, b.no_return,
        [&](const RayEvent& e) {
            Cell& c = cells_[e.index];
            if (e.occupied) {
                c.logit = std::min(cfg_.l_max, c.logit + cfg_.l_occ);
                ++stats.occupied_updates;
            } else {
                c.logit = std::max(cfg_.l_min, c.logit + cfg_.l_free);
                ++stats.free_updates;
            }
            c.observed = true;
        });
    if (clipped) ++stats.beams_clipped;
}

ScanStats GridMap::update(const Scan& scan, bool reference) {
    const double max_range =
        scan.max_range > 0.0 ? scan.max_range : cfg_.default_max_range;

    const Eigen::Index m = static_cast<Eigen::Index>(scan.beams.size());

    // World-frame beam geometry computed in batch with Eigen: bearings,
    // unit direction components and each beam's travel length.
    Eigen::VectorXd angles(m), ranges(m);
    for (Eigen::Index i = 0; i < m; ++i) {
        const Beam& bm = scan.beams[static_cast<std::size_t>(i)];
        angles(i) = scan.pose.theta + bm.angle;
        ranges(i) = bm.no_return ? max_range : bm.range;
    }
    const Eigen::VectorXd dx = angles.array().cos();
    const Eigen::VectorXd dy = angles.array().sin();

    ScanStats stats;
    stats.beams = static_cast<int>(m);

    TraversalFn traverse =
        reference ? traceBeamSampled : traceBeamAmanatidesWoo;

    for (Eigen::Index i = 0; i < m; ++i) {
        const Beam& bm = scan.beams[static_cast<std::size_t>(i)];
        // A no-return beam: frees only up to max range, never occupied.
        PreparedBeam pb{scan.pose.x,
                        scan.pose.y,
                        dx(i),
                        dy(i),
                        ranges(i),
                        bm.no_return};
        if (bm.no_return)
            ++stats.no_returns;
        else
            ++stats.hits;
        applyBeam(pb, traverse, stats);
    }
    return stats;
}

std::vector<double> GridMap::probabilities() const {
    const std::size_t n = cells_.size();
    // p = 1 / (1 + exp(-l)), stable for all finite inputs.
    Eigen::VectorXd logits(n);
    for (std::size_t i = 0; i < n; ++i) logits[static_cast<Eigen::Index>(i)] =
        cells_[i].observed ? cells_[i].logit : 0.0;
    Eigen::VectorXd p =
        1.0 / (1.0 + (-logits.array()).exp());
    std::vector<double> out(n);
    for (std::size_t i = 0; i < n; ++i)
        out[i] = p[static_cast<Eigen::Index>(i)];
    return out;
}

std::vector<float> GridMap::logOdds() const {
    std::vector<float> out(cells_.size(), 0.0f);
    for (std::size_t i = 0; i < cells_.size(); ++i)
        out[i] = cells_[i].observed ? static_cast<float>(cells_[i].logit)
                                    : 0.0f;
    return out;
}

GridMap::ReferenceDiff GridMap::verifyAgainstReference(
    const MapConfig& cfg, const std::vector<Scan>& scans) {
    GridMap prod(cfg), ref(cfg);
    for (const Scan& s : scans) {
        prod.update(s, false);
        ref.update(s, true);
    }
    ReferenceDiff d{0, 0, 0.0, {}};
    d.cell_count = static_cast<int>(prod.cells_.size());
    for (std::size_t i = 0; i < prod.cells_.size(); ++i) {
        const Cell& a = prod.cells_[i];
        const Cell& b = ref.cells_[i];
        const double diff = std::fabs(static_cast<double>(a.logit) -
                                      static_cast<double>(b.logit));
        const bool mismatch =
            a.observed != b.observed || diff > 1e-9;
        if (mismatch) {
            ++d.differing_cells;
            d.max_abs_logit_diff = std::max(d.max_abs_logit_diff, diff);
            if (d.first_mismatches.size() < 10)
                d.first_mismatches.push_back(static_cast<int>(i));
        }
    }
    return d;
}

}  // namespace gridfusion
