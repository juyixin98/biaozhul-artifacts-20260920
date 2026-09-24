#pragma once
// Occupancy grid with log-odds fusion.
//
// A GridMap instance is one immutable map geometry + sensor model plus the
// mutable per-cell log-odds state. Cells that have never been touched are
// indistinguishable in value from a posterior of exactly 0.5, so occupancy
// is tracked together with an "observed" flag — the export API distinguishes
// unknown cells (never observed) from observed cells sitting at 0.5.

#include <cstdint>
#include <string>
#include <vector>

#include <Eigen/Core>

#include "gridfusion/raycast.hpp"
#include "gridfusion/types.hpp"

namespace gridfusion {

struct Cell {
    double logit = 0.0;
    bool observed = false;
};

class GridMap {
public:
    explicit GridMap(const MapConfig& cfg);

    const MapConfig& config() const { return cfg_; }
    int width() const { return cfg_.width; }
    int height() const { return cfg_.height; }

    // Version id: real SHA-256 over the canonical geometry string
    // ("res|ox|oy|w|h"). Beams from a map with a different resolution/origin
    // may not be mixed in — the registry matches this id.
    const std::string& version_id() const { return version_id_; }
    static std::string canonical_geometry(const MapConfig& cfg);
    static std::string make_version_id(const MapConfig& cfg);

    // Apply a scan with the production (Amanatides-Woo) traversal.
    ScanStats update(const Scan& scan) { return update(scan, false); }

    // Apply a scan with the independent dense-sampling reference traversal.
    // Updates are applied in exactly the per-beam, per-event order the
    // traversal emits, so verifyAgainstReference can demand exact equality.
    ScanStats updateReference(const Scan& scan) { return update(scan, true); }

    // Replay every scan on a fresh map with both traversals and report the
    // largest absolute logit difference and how many cells differ.
    struct ReferenceDiff {
        int cell_count;
        int differing_cells;
        double max_abs_logit_diff;
        std::vector<int> first_mismatches;  // up to a handful of cell indices
    };
    static ReferenceDiff verifyAgainstReference(
        const MapConfig& cfg, const std::vector<Scan>& scans);

    const std::vector<Cell>& cells() const { return cells_; }
    const Cell& cell(int x, int y) const { return cells_[y * cfg_.width + x]; }

    // ---- coordinate helpers ----
    int worldToCellX(double x) const;
    int worldToCellY(double y) const;
    bool poseInside(const Pose& p) const;

    // ---- probability export (Eigen-vectorised) ----
    // Posterior probabilities for observed cells, 0.5 for unknown.
    std::vector<double> probabilities() const;
    std::vector<float> logOdds() const;

    void reset();

private:
    using TraversalFn = bool (*)(double, double, double, double, double, int,
                                 int, double, double, double, bool,
                                 const EventSink&);

    ScanStats update(const Scan& scan, bool reference);
    void applyBeam(const PreparedBeam& b, TraversalFn traverse,
                   ScanStats& stats);

    MapConfig cfg_;
    std::string version_id_;
    std::vector<Cell> cells_;
};

double logitFromProbability(double p);

}  // namespace gridfusion
