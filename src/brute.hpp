// Naive exhaustive reference implementations, intended ONLY for small
// graphs (used by tests and, on request, for graphs within the brute
// force size limit). These deliberately avoid any clever algorithm so
// that they independently cross-check the Hopcroft-Karp / Konig result.
#pragma once

#include <cstddef>
#include <vector>

#include "bipartite.hpp"

namespace brute {

// Upper bounds that keep exhaustive enumeration comfortably fast.
constexpr size_t kMaxTotalVertices = 20;  // 2^20 subsets in the worst case
constexpr size_t kMaxEdges = 80;

struct ReferenceResult {
  size_t maxMatchingSize = 0;
  size_t minVertexCoverSize = 0;
  // One minimum cover found by enumeration (dense indices mapped later
  // by the caller if needed; here original ids are returned directly).
  std::vector<bipartite::Solution::CoverVertex> minVertexCover;
};

// Returns false when the instance exceeds the brute force limits.
bool withinLimits(size_t leftCount, size_t rightCount, size_t edgeCount);

// Exhaustively searches all matchings (backtracking over left vertices)
// and returns the largest size found.
size_t exhaustiveMatchingSize(
    const std::vector<bipartite::Edge>& edges,
    const std::vector<long long>& leftVertices,
    const std::vector<long long>& rightVertices);

// Enumerates every subset of the union of all vertices and returns the
// cardinality (and an example) of the smallest subset covering every
// edge. Independent of matching theory: a direct definition check.
ReferenceResult exhaustiveMinVertexCover(
    const std::vector<bipartite::Edge>& edges,
    const std::vector<long long>& leftVertices,
    const std::vector<long long>& rightVertices);

}  // namespace brute
