// Polygon construction/validation and point location engines.
#pragma once

#include <cstdint>
#include <string>
#include <vector>

#include "geometry.hpp"

namespace locator {

using geo::Location;
using geo::Point;
using geo::i64;

struct Polygon {
    std::vector<Point> outer;
    std::vector<std::vector<Point>> holes;
    size_t totalVertices() const;
};

enum class BuildErrorKind {
    InvalidRing,
    HoleOutsideOuter,
    HolesIntersectOrNested,
    TooManyVertices,
};

struct BuildError {
    BuildErrorKind kind;
    std::string which;          // "outer" or "holes[3]"
    geo::RingError ringError;   // valid when kind == InvalidRing
    std::string detail;
};

// Per-ring validity is checked first; then containment/non-overlap between
// rings. Returns nullptr on success.
bool buildPolygon(std::vector<Point> outer,
                  std::vector<std::vector<Point>> holes,
                  Polygon& out, BuildError& err);

// Exact, orientation-independent classification.
Location locateNaive(const Polygon& poly, const Point& p);

// Horizontal slab index built once, used for repeated batch queries.
class GridIndex {
public:
    void build(const Polygon& poly);
    Location query(const Point& p) const;

    int bandCount() const { return bandCount_; }
    size_t totalEdgeSlots() const;
    size_t edgeCount() const { return edgeCount_; }
    uint64_t candidatesExamined() const { return examined_; }

private:
    struct Edge { Point a, b; };
    struct RingData {
        std::vector<Edge> edges;
        std::vector<std::vector<uint32_t>> bands; // index into edges
    };

    std::vector<RingData> rings_; // 0 = outer, rest = holes
    int bandCount_ = 1;
    i64 y0_ = 0, bandH_ = 1;
    i64 xmin_ = 0, xmax_ = 0, ymin_ = 0, ymax_ = 0;
    size_t edgeCount_ = 0;
    mutable uint64_t examined_ = 0;

    int bandOf(i64 y) const;
};

} // namespace locator
