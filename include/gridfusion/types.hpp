#pragma once
// Core value types for 2D occupancy-grid fusion.

#include <cstdint>
#include <string>
#include <vector>

namespace gridfusion {

// Fixed geometry + sensor model of one map. The geometry part
// (resolution, origin, size) is hashed into the version id; sensor-model
// parameters are immutable companions of the same map but are deliberately
// NOT part of the hash (see GridMap::version_id semantics in grid_map.hpp).
struct MapConfig {
    double resolution = 0.1;     // metres per cell edge, > 0
    double origin_x = 0.0;       // world coordinates of cell (0,0) centre
    double origin_y = 0.0;
    int width = 0;               // cells along x, > 0
    int height = 0;              // cells along y, > 0

    double l_occ = 0.8473;       // log-odds added by a hit (logit(0.7))
    double l_free = -0.4055;     // log-odds added along a ray (logit(0.4))
    double l_min = -2.0;         // saturation lower bound (~0.119)
    double l_max = 3.5;          // saturation upper bound (~0.971)
    double default_max_range = 20.0;  // used when a scan omits max_range, > 0

    // Strict validation used by every input path. Returns "" when valid.
    std::string validate() const;
    // Geometry-only exact equality (what a matching version id guarantees).
    bool same_geometry(const MapConfig& o) const;
    // Full equality including sensor parameters.
    bool operator==(const MapConfig& o) const;
};

struct Pose {
    double x = 0.0;
    double y = 0.0;
    double theta = 0.0;  // radians, counter-clockwise, beam 0 = +x
};

// One range reading.
struct Beam {
    double angle = 0.0;  // relative to pose.theta, radians
    double range = 0.0;  // metres; must be >= 0
    // true => no return ("max range beam"): trace frees only, never mark an
    // endpoint occupied, capped at max_range.
    bool no_return = false;
};

struct Scan {
    Pose pose;
    double max_range = -1.0;  // <= 0 means "use map default"
    std::vector<Beam> beams;
};

// Per-scan counters returned by the update API.
struct ScanStats {
    int beams = 0;
    int hits = 0;            // returned beams
    int no_returns = 0;      // max-range beams
    int beams_clipped = 0;   // endpoint/ray ended on the map boundary
    std::uint64_t occupied_updates = 0;
    std::uint64_t free_updates = 0;
};

// One prepared beam in world coordinates: sensor S, unit direction, travel.
struct PreparedBeam {
    double sx, sy, dx, dy;
    double travel;
    bool no_return;
};

}  // namespace gridfusion
