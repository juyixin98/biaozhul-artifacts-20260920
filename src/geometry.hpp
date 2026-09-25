// geometry.hpp — planar Cartesian primitives for the simplification backend.
//
// Coordinate convention:
//   All coordinates are treated as 2D planar Cartesian coordinates in whatever
//   unit and CRS the caller declares in the request ("crs" field, informational
//   only). No reprojection, geodesic correction, or datum handling is performed.
//   Distances are Euclidean in the same unit as the input coordinates.
#pragma once

#include <cmath>
#include <vector>

namespace simp {

struct Point {
    double x = 0.0;
    double y = 0.0;
};

// Distance from p to the closed segment [a, b].
// Degenerate case: if a == b (zero-length segment, e.g. duplicate consecutive
// points or a closed ring whose first and last vertices coincide), the distance
// falls back to the plain point-to-point distance |p - a|.
inline double dist_point_segment(const Point& p, const Point& a, const Point& b) {
    const double dx = b.x - a.x;
    const double dy = b.y - a.y;
    const double len_sq = dx * dx + dy * dy;
    if (len_sq == 0.0) {
        return std::hypot(p.x - a.x, p.y - a.y);
    }
    // Projection parameter of p onto the line through a,b, clamped to [0,1].
    double t = ((p.x - a.x) * dx + (p.y - a.y) * dy) / len_sq;
    if (t < 0.0) t = 0.0;
    if (t > 1.0) t = 1.0;
    const double proj_x = a.x + t * dx;
    const double proj_y = a.y + t * dy;
    return std::hypot(p.x - proj_x, p.y - proj_y);
}

// Distance from p to a polyline: minimum over all segments.
// A polyline with a single vertex degenerates to that point.
inline double dist_point_polyline(const Point& p, const std::vector<Point>& line) {
    if (line.empty()) {
        return -1.0; // caller must not invoke this on an empty polyline
    }
    if (line.size() == 1) {
        return std::hypot(p.x - line[0].x, p.y - line[0].y);
    }
    double best = -1.0;
    for (std::size_t i = 0; i + 1 < line.size(); ++i) {
        const double d = dist_point_segment(p, line[i], line[i + 1]);
        if (best < 0.0 || d < best) best = d;
    }
    return best;
}

} // namespace simp
