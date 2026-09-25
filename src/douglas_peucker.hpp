// douglas_peucker.hpp — Douglas-Peucker polyline simplification.
//
// Guarantees (one-directional only):
//   After simplification with tolerance t, every vertex of the ORIGINAL
//   polyline lies within distance t of the SIMPLIFIED polyline (directed
//   Hausdorff distance original -> simplified <= t, in exact arithmetic).
//   This is verified per point in the validation report.
//
// Explicitly NOT claimed:
//   The reverse direction (simplified -> original) is NOT bounded by t, and
//   the symmetric (bidirectional) Hausdorff distance is therefore NOT bounded
//   by t either. A long detour of the original polyline can pass arbitrarily
//   close to a simplified segment's endpoints region... concretely: simplified
//   segments are chords of the original chain, and points ON those chords may
//   be far from every original vertex. No such bound is asserted anywhere in
//   this project.
//
// Determinism:
//   The farthest-point scan uses a strict ">" comparison, so on ties the
//   lowest index wins; the recursion is an explicit stack processed in a fixed
//   order. Same input + same tolerance => byte-identical output.
#pragma once

#include <cstddef>
#include <utility>
#include <vector>

#include "geometry.hpp"

namespace simp {

// Returns the sorted indices of the kept vertices. Endpoints (index 0 and
// n-1) are always kept when n >= 1. With tolerance == 0, every vertex with a
// strictly positive distance to its collapsing chord is kept; vertices lying
// exactly on a chord (distance exactly 0.0) are dropped. That is deterministic
// and documented; pass a tiny epsilon if you want to keep near-collinear points.
inline std::vector<std::size_t> douglas_peucker(const std::vector<Point>& pts,
                                                double tolerance) {
    const std::size_t n = pts.size();
    std::vector<std::size_t> result;
    if (n == 0) return result;
    if (n <= 2) {
        for (std::size_t i = 0; i < n; ++i) result.push_back(i);
        return result;
    }

    std::vector<char> keep(n, 0);
    keep[0] = 1;
    keep[n - 1] = 1;

    // Explicit stack of [first, last] index ranges; avoids recursion-depth
    // issues on degenerate inputs (e.g. long backtracking chains).
    std::vector<std::pair<std::size_t, std::size_t>> stack;
    stack.push_back({0, n - 1});
    while (!stack.empty()) {
        const auto range = stack.back();
        stack.pop_back();
        const std::size_t first = range.first;
        const std::size_t last = range.second;

        double dmax = -1.0;
        std::size_t split = first;
        for (std::size_t k = first + 1; k < last; ++k) {
            const double d = dist_point_segment(pts[k], pts[first], pts[last]);
            if (d > dmax) { // strict: first (lowest-index) maximum wins ties
                dmax = d;
                split = k;
            }
        }
        if (dmax > tolerance) {
            keep[split] = 1;
            stack.push_back({first, split});
            stack.push_back({split, last});
        }
    }

    for (std::size_t i = 0; i < n; ++i) {
        if (keep[i]) result.push_back(i);
    }
    return result;
}

} // namespace simp
