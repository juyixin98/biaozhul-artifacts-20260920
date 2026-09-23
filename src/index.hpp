// Preprocessed point-location index.
//
// Horizontal slab decomposition:
//   * distinct vertex y coordinates define ordered "levels";
//   * each open slab between consecutive levels stores the non-horizontal
//     edges that span it, sorted by the x of their intersection with the
//     slab's midline (exact rational ordering);
//   * sorted edge lists are produced by a sweep: the active edge set only
//     changes at a level (edges start/end there), so each new slab is the
//     previous list with ending edges removed and starting edges inserted by
//     exact binary search — never re-sorted from scratch.
//
// Edges live in one pool; slab/level lists store integer edge IDs (cheap to
// permute). A query locates its slab with binary search and counts crossings
// with another binary search. Boundary is always tested first. Everything is
// exact integer/rational arithmetic with cpp_int.
#pragma once

#include <cstdint>
#include <vector>

#include "geometry.hpp"

struct IndexEdge {
    Pt a, b; // stored with a.y < b.y (non-horizontal)
};

struct RingIndex {
    const Ring* ring = nullptr;
    Pt lo{}, hi{};                            // bounding box
    std::vector<Int> levels;                 // sorted unique y
    std::vector<IndexEdge> pool;             // non-horizontal (upward) edges
    std::vector<std::vector<int32_t>> startAt; // startAt[k]: edge IDs with a.y == levels[k]
    std::vector<std::vector<int32_t>> endAt;   // endAt[k]:   edge IDs with b.y == levels[k]
    std::vector<std::vector<int32_t>> slab;    // slab[k]: sorted active edge IDs
    std::vector<std::vector<IndexEdge>> levelHoriz; // horizontal edges at levels[k]
    std::vector<std::vector<size_t>> levelVerts;   // vertex indices at levels[k]

    void build(const Ring& r);
};

struct PolygonIndex {
    const Polygon* polygon = nullptr;
    RingIndex outer;
    std::vector<RingIndex> hole;

    void build(const Polygon& p);
};

namespace point_index {
// Indexed query for one ring.
Rel locateRing(const RingIndex& idx, const Pt& p);
// Indexed query for a polygon with holes.
Rel locatePolygon(const PolygonIndex& idx, const Pt& p);
} // namespace point_index
