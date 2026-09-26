#pragma once

#include <cstdint>
#include <vector>

#include "graph.hpp"

namespace mcut {

struct BruteForceResult {
  std::int64_t min_cut_value = 0;
  // One attaining partition; bit v is 1 iff v belongs to the source side.
  // s always set, t always clear.
  std::vector<unsigned char> source_side;
  // How many of the 2^(n-2) s-t partitions were enumerated.
  std::uint64_t partitions_checked = 0;
};

// Naive reference: enumerate EVERY s-t partition (all 2^(n-2) of them) and
// sum the capacities of original edges directed from S to T. No flow is
// involved at all, which makes this an independent ground truth for the
// min-cut value. Parallel edges count separately and zero-capacity edges
// contribute 0 automatically. Only intended for n <= kBruteMaxVertices.
BruteForceResult brute_force_min_cut(const Problem& problem);

}  // namespace mcut
