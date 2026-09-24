// Core data types for grid probability fusion.
#pragma once

#include <Eigen/Dense>
#include <cstdint>
#include <string>
#include <vector>

namespace pf {

// World-space 2D pose of the sensor: position + heading.
// Beam world origins/endpoints are derived as
//   o = (x, y),  end = o + range * (cos(theta+angle), sin(theta+angle))
struct Pose {
    double x = 0.0;
    double y = 0.0;
    double theta = 0.0;  // radians, CCW from +X
};

// A single lidar beam expressed in *world* coordinates.
struct Beam {
    double ox = 0.0;  // origin x
    double oy = 0.0;  // origin y
    // Valid return: endpoint of the ray (the cell it lands in is marked
    // OCCUPIED, open segment cells are marked FREE).
    double ex = 0.0;
    double ey = 0.0;
    bool hit = true;  // false => no return (max-range beam): free-only
    double max_range = 0.0;  // used only when hit == false
};

// Immutable geometric configuration of a map/grid.
struct MapConfig {
    int width = 0;          // cells along x (columns)
    int height = 0;         // cells along y (rows)
    double resolution = 0.1;          // metres per cell
    double origin_x = 0.0;  // world coordinates of the (0,0) cell *corner*
    double origin_y = 0.0;
};

struct ScanInput {
    Pose pose;
    std::vector<Beam> beams;
};

struct UpdateCounts {
    std::uint64_t free_cells = 0;
    std::uint64_t occupied_cells = 0;
};

}  // namespace pf
