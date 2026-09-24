// OccupancyGrid: fixed-size 2D log-odds grid with an Amanatides-Woo
// voxel-traversal ray caster.
//
// Semantics (see README "融合规则"):
//   * Hit beam: every cell on the OPEN segment origin->endpoint gets one free
//     update; the cell containing the endpoint gets one occupied update.
//   * No-return beam: cells along [origin, origin + max_range*dir) get free
//     updates only; nothing is marked occupied.
//   * Cells outside the map are skipped (a ray that leaves the convex grid
//     cannot re-enter).
//   * Observed cells (ever touched) are tracked separately from unknown
//     cells, so an observed cell sitting exactly at the 0.5 prior is not
//     confused with an never-seen cell.
//   * Log-odds are clamped to +-l_max after every touch (bounded saturation).
#pragma once

#include "types.hpp"

#include <Eigen/Dense>

#include <cstdint>
#include <string>
#include <vector>

namespace pf {

// Immutable parameters of one fusion map (bound to its version chain).
struct FusionParams {
    double l_free = -0.40546510810816438;  // logit(0.4)  = log(2/3)
    double l_hit = 0.40546510810816438;    // logit(0.6)  = log(3/2)
    double l_max = 3.0;
};

struct GridSnapshot {
    std::vector<double> log_odds;        // row-major (iy outer), size W*H
    std::vector<std::uint8_t> observed;  // 1 if ever touched
    std::vector<std::uint64_t> free_touches;
    std::vector<std::uint64_t> hit_touches;
};

class OccupancyGrid {
public:
    OccupancyGrid(MapConfig cfg, FusionParams params);

    const MapConfig& config() const { return cfg_; }
    const FusionParams& params() const { return params_; }

    // Apply one scan (all beams in order). Returns touch counts.
    UpdateCounts applyScan(const ScanInput& scan);

    // Single-beam reference update; exposed so tests can compare a
    // beam-by-beam reference implementation against batched scans.
    UpdateCounts applyBeam(const Beam& beam);

    // --- accessors ---
    bool inside(int ix, int iy) const {
        return ix >= 0 && iy >= 0 && ix < cfg_.width && iy < cfg_.height;
    }
    int index(int ix, int iy) const { return iy * cfg_.width + ix; }
    // Floor cell indices for a world point (may be out of bounds).
    void worldToCell(double wx, double wy, int& ix, int& iy) const;

    double logOdds(int ix, int iy) const { return lo_(index(ix, iy)); }
    bool observed(int ix, int iy) const { return obs_(index(ix, iy)) != 0; }
    // p = sigmoid(log-odds); valid only for observed cells.
    double probability(int ix, int iy) const;

    std::uint64_t hitCount(int ix, int iy) const { return hit_n_(index(ix, iy)); }
    std::uint64_t freeCount(int ix, int iy) const { return free_n_(index(ix, iy)); }

    GridSnapshot snapshot() const;

    // Canonical digest over the ENTIRE current grid state:
    //   "GRID-STATE:v1\nres=..\norigin=..\nsize=W,H\n" then one
    //   "ix,iy,L,O\n" line per cell (row-major), L = integer micro-log-odds
    //   round(lo*1e6), O in {0,1}.
    std::string stateDigest() const;
    static std::string stateDigestFrom(const MapConfig& cfg,
                                       const GridSnapshot& snap);

    // Canonical textual form of the immutable configuration.
    static std::string configCanonical(const MapConfig& cfg,
                                       const FusionParams& p);

    static double logit(double p);
    static double sigmoid(double l);

private:
    void touchFree(int cell);
    void touchHit(int cell);

    // Traces one beam; free-only when hit==false.
    UpdateCounts castBeam(const Beam& beam);

    MapConfig cfg_;
    FusionParams params_;

    Eigen::VectorXd lo_;                    // log-odds, 0 = prior
    Eigen::Matrix<std::uint8_t, Eigen::Dynamic, 1> obs_;
    Eigen::Matrix<std::uint64_t, Eigen::Dynamic, 1> hit_n_;
    Eigen::Matrix<std::uint64_t, Eigen::Dynamic, 1> free_n_;
};

}  // namespace pf
